#!/usr/bin/env python
# -*- coding: UTF-8 -*-
# File: imagenet-resnet.py

import sys
import argparse
import numpy as np
import os
import multiprocessing

import tensorflow as tf

from tensorpack import *
from tensorpack.tfutils.symbolic_functions import *
from tensorpack.tfutils.summary import *

from imagenet_resnet_utils import (
    fbresnet_augmentor, resnet_basicblock, resnet_bottleneck, resnet_backbone,
    eval_on_ILSVRC12, image_preprocess, compute_loss_and_error)

TOTAL_BATCH_SIZE = 256
INPUT_SHAPE = 224
DEPTH = None

RESNET_CONFIG = {
    18: ([2, 2, 2, 2], resnet_basicblock),
    34: ([3, 4, 6, 3], resnet_basicblock),
    50: ([3, 4, 6, 3], resnet_bottleneck),
    101: ([3, 4, 23, 3], resnet_bottleneck)
}


class Model(ModelDesc):
    def __init__(self, data_format='NCHW'):
        if data_format == 'NCHW':
            assert tf.test.is_gpu_available()
        self.data_format = data_format

    def _get_inputs(self):
        # uint8 instead of float32 is used as input type to reduce copy overhead.
        # It might hurt the performance a liiiitle bit.
        # The pretrained models were trained with float32.
        return [InputDesc(tf.uint8, [None, INPUT_SHAPE, INPUT_SHAPE, 3], 'input'),
                InputDesc(tf.int32, [None], 'label')]

    def _build_graph(self, inputs):
        image, label = inputs
        # It should actually use bgr=True here, but for compatibility with
        # pretrained models, we keep the wrong version.
        image = image_preprocess(image, bgr=False)

        if self.data_format == 'NCHW':
            image = tf.transpose(image, [0, 3, 1, 2])
        defs, block_func = RESNET_CONFIG[DEPTH]

        with argscope([Conv2D, MaxPooling, GlobalAvgPooling, BatchNorm], data_format=self.data_format):
            logits = resnet_backbone(image, defs, block_func)

        loss = compute_loss_and_error(logits, label)

        wd_loss = regularize_cost('.*/W', l2_regularizer(1e-4), name='l2_regularize_loss')
        add_moving_summary(loss, wd_loss)
        self.cost = tf.add_n([loss, wd_loss], name='cost')

    def _get_optimizer(self):
        lr = get_scalar_var('learning_rate', 0.1, summary=True)
        return tf.train.MomentumOptimizer(lr, 0.9, use_nesterov=True)


def get_data(train_or_test):
    isTrain = train_or_test == 'train'

    datadir = args.data
    ds = dataset.ILSVRC12(datadir, train_or_test,
                          shuffle=isTrain, dir_structure='original')
    augmentors = fbresnet_augmentor(isTrain)
    augmentors.append(imgaug.ToUint8())

    ds = AugmentImageComponent(ds, augmentors, copy=False)
    if isTrain:
        ds = PrefetchDataZMQ(ds, min(20, multiprocessing.cpu_count()))
    ds = BatchData(ds, BATCH_SIZE, remainder=not isTrain)
    return ds


def get_config(fake=False, data_format='NCHW'):
    nr_tower = max(get_nr_gpu(), 1)
    global BATCH_SIZE
    BATCH_SIZE = TOTAL_BATCH_SIZE // nr_tower

    if fake:
        logger.info("For benchmark, batch size is fixed to 64 per tower.")
        dataset_train = dataset_val = FakeData(
            [[64, 224, 224, 3], [64]], 1000, random=False, dtype='uint8')
    else:
        logger.info("Running on {} towers. Batch size per tower: {}".format(nr_tower, BATCH_SIZE))
        dataset_train = get_data('train')
        dataset_val = get_data('val')

    return TrainConfig(
        model=Model(data_format=data_format),
        dataflow=dataset_train,
        callbacks=[
            ModelSaver(),
            InferenceRunner(dataset_val, [
                ClassificationError('wrong-top1', 'val-error-top1'),
                ClassificationError('wrong-top5', 'val-error-top5')]),
            ScheduledHyperParamSetter('learning_rate',
                                      [(30, 1e-2), (60, 1e-3), (85, 1e-4), (95, 1e-5), (105, 1e-6)]),
            HumanHyperParamSetter('learning_rate'),
        ],
        steps_per_epoch=5000,
        max_epoch=110,
        nr_tower=nr_tower
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--gpu', help='comma separated list of GPU(s) to use.')
    parser.add_argument('--data', help='ILSVRC dataset dir')
    parser.add_argument('--load', help='load model')
    parser.add_argument('--fake', help='use fakedata to test or benchmark this model', action='store_true')
    parser.add_argument('--data_format', help='specify NCHW or NHWC',
                        type=str, default='NCHW')
    parser.add_argument('-d', '--depth', help='resnet depth',
                        type=int, default=18, choices=[18, 34, 50, 101])
    parser.add_argument('--eval', action='store_true')
    args = parser.parse_args()

    DEPTH = args.depth
    if args.gpu:
        os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu

    if args.eval:
        BATCH_SIZE = 128    # something that can run on one gpu
        ds = get_data('val')
        eval_on_ILSVRC12(Model(), args.load, ds)
        sys.exit()

    logger.set_logger_dir(
        os.path.join('train_log', 'imagenet-resnet-d' + str(DEPTH)))
    config = get_config(fake=args.fake, data_format=args.data_format)
    if args.load:
        config.session_init = SaverRestore(args.load)
    SyncMultiGPUTrainerParameterServer(config).train()

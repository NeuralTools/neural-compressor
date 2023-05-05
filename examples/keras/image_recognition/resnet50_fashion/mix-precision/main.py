#
# -*- coding: utf-8 -*-
#
# Copyright (c) 2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import time
import numpy as np
import tensorflow as tf
from neural_compressor.utils import logger
tf.compat.v1.logging.set_verbosity(tf.compat.v1.logging.ERROR)

flags = tf.compat.v1.flags
FLAGS = flags.FLAGS

## Required parameters
flags.DEFINE_string(
    'input_model', None, 'Run inference with specified keras model.')

flags.DEFINE_string(
    'output_model', None, 'The output quantized model.')

flags.DEFINE_string(
    'mode', 'performance', 'define benchmark mode for accuracy or performance')

flags.DEFINE_bool(
    'tune', False, 'whether to tune the model')

flags.DEFINE_bool(
    'benchmark', False, 'whether to benchmark the model')

flags.DEFINE_string(
    'calib_data', None, 'location of calibration dataset')

flags.DEFINE_string(
    'eval_data', None, 'location of evaluate dataset')

flags.DEFINE_integer('batch_size', 32, 'batch_size')

flags.DEFINE_integer(
    'iters', 100, 'maximum iteration when evaluating performance')

from neural_compressor import Metric

def evaluate(model):
    """Custom evaluate function to inference the model for specified metric on validation dataset.

    Args:
        model (tf.saved_model.load): The input model will be the class of tf.saved_model.load(quantized_model_path).

    Returns:
        accuracy (float): evaluation result, the larger is better.
    """
    from neural_compressor import METRICS
    metrics = METRICS('tensorflow')
    metric = metrics['topk']()

    def eval_func(data_loader, metric):
        warmup = 5
        iteration = None
        latency_list = []
        if FLAGS.benchmark and FLAGS.mode == 'performance':
            iteration = FLAGS.iters
        for idx, (inputs, labels) in enumerate(data_loader):
            start = time.time()
            predictions = model.predict_on_batch(inputs)
            end = time.time()
            metric.update(predictions, labels)
            latency_list.append(end - start)
            if iteration and idx >= iteration:
                break
        latency = np.array(latency_list[warmup:]).mean() / dataloader.batch_size
        return latency

    from neural_compressor.utils.create_obj_from_config import create_dataloader
    dataloader_args = {
        'batch_size': FLAGS.batch_size,
        'dataset': {"FashionMNIST": {'root':FLAGS.eval_data}},
        'transform': {'Rescale': {}},
        'filter': None
    }
    dataloader = create_dataloader('tensorflow', dataloader_args)
    latency = eval_func(dataloader, metric)
    if FLAGS.benchmark and FLAGS.mode == 'performance':
        print("Batch size = {}".format(dataloader.batch_size))
        print("Latency: {:.3f} ms".format(latency * 1000))
        print("Throughput: {:.3f} images/sec".format(1. / latency))
    acc = metric.result()
    return acc

def main(_):
    if FLAGS.tune:
        from neural_compressor.config import MixedPrecisionConfig
        from neural_compressor import mix_precision
        # add backend='itex' to run on keras adaptor
        config = MixedPrecisionConfig(backend='itex')

        bf16_model = mix_precision.fit(
            model=FLAGS.input_model,
            config=config,
            eval_func=evaluate)
        bf16_model.save(FLAGS.output_model)

    if FLAGS.benchmark:
        from neural_compressor.benchmark import fit
        from neural_compressor.config import BenchmarkConfig
        if FLAGS.mode == 'performance':
            conf = BenchmarkConfig(backend='itex', cores_per_instance=4, num_of_instance=7)
            fit(FLAGS.input_model, conf, b_func=evaluate)
        else:
            from neural_compressor.model.model import Model
            accuracy = evaluate(Model(FLAGS.input_model, backend='keras').model)
            logger.info('Batch size = %d' % FLAGS.batch_size)
            logger.info("Accuracy: %.5f" % accuracy)

if __name__ == "__main__":
    tf.compat.v1.app.run()

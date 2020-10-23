"""Tests for config file"""
import unittest
import numpy as np
import yaml
import os
from ilit.conf import config as conf


def helper(content):
    with open('fake_conf.yaml', 'w', encoding="utf-8") as f:
        f.write(content)


class TestConf(unittest.TestCase):
    @classmethod
    def tearDownClass(self):
        os.remove('fake_conf.yaml')

    def test_main_key(self):
        test = '''
        model:
          name: main_key_yaml
          framework: pytorch
        test: cpu
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_framework(self):
        test = '''
        model:
          name: framework_yaml 
          framework: pytorch, mxnet
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        device: cpu
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_device(self):
        test = '''
        model:
          name: device_yaml 
          framework: mxnet
        device: xpu
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: device_yaml 
          framework: tensorflow
        device: cpu
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: device_yaml 
          framework: mxnet
        device: cpu, gpu
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_calibration(self):
        test = '''
        model:
          name: calib_yaml 
          framework: mxnet
        quantization:
          calibration:
            sampling_sizes: 10
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: calib_yaml 
          framework: mxnet
        quantization:
          calibration:
            sampling_size:
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: calib_yaml 
          framework: mxnet
        quantization:
          calibration:
            dataloader:
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: calib_yaml 
          framework: mxnet
        quantization:
          calibration:
          op_wise: {
            'test': {
                'activation': [{'dtype': 'uint8'}, {'algorithm': 'minmax'}]
            }
          }

        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_quantization(self):
        test = '''
        model:
          name: quant_yaml 
          framework: mxnet
        quantization:
          model_wise:
            weights:
            granularity: per_channel
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: quant_yaml 
          framework: mxnet
        quantization:
          model_wise:
          approach:
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: quant_yaml 
          framework: mxnet
        quantization:
          approach: post_training_static_quant, quant_aware_training
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: quant_yaml 
          framework: mxnet
        quantization:
          model_wise:
            activation:
              scheme: asym
              dtype: int8
            weight:
              scheme: asym
              dtype: int8
        '''
        helper(test)
        conf.Conf('fake_conf.yaml')

        test = '''
        model:
          name: quant_yaml 
          framework: mxnet
        quantization:
          model_wise:
            activation:
              scheme:
              dtype: int8
            weight:
              scheme: asym
              dtype: int8
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_tuning(self):
        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
            relative: 0.01
          strategy:
            name: basic, mse
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
          relative: 0.01
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
          relative: 0.01
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
            relative: 0.01
          strategy:
            name: fake
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
            relative:
          strategy:
            name: basic
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
          exit_policy:
            timeout: 3
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

        test = '''
        model:
          name: tuning_yaml 
          framework: mxnet
        tuning:
          accuracy_criterion:
            relative: 0.01
            absolute: 0.01
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_snapshot(self):
        test = '''
        model:
          name: snapshot_yaml 
          framework: mxnet
        tuning:
          snapshot: 
            -path: /path/to/snapshot
        '''
        helper(test)
        self.assertRaises(RuntimeError, conf.Conf, 'fake_conf.yaml')

    def test_inputs_outputs(self):
        test = '''
        model:
          name: inout_yaml 
          framework: mxnet
          inputs: x, y
        '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')
        self.assertEqual(config.usr_cfg.model.inputs, ['x', 'y'])

    def test_modelwise_conf_merge(self):
        test = '''
        model:
          name: inout_yaml 
          framework: mxnet
        quantization:
          model_wise:
            weight:
              algorithm:  minmax
            activation:
              algorithm:  minmax
        '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')

        framework_modelwise_capability = {
            'activation': {
                'dtype': ['uint8', 'fp32'],
                'scheme': ['asym', 'sym'],
                'granularity': ['per_tensor'],
                'algorithm': ['minmax', 'kl']
            },
            'weight': {
                'dtype': ['int8', 'fp32'],
                'scheme': [
                    'sym',
                ],
                'granularity': ['per_channel', 'per_tensor'],
                'algorithm': ['minmax']
            },
        }

        tune_space = config.modelwise_tune_space(framework_modelwise_capability)
        self.assertEqual(tune_space['activation']['algorithm'], ['minmax'])

    def test_ops_override(self):
        test = '''
        model:
          name: ops_override_yaml 
          framework: mxnet
        quantization:
          op_wise: {
            'conv1': {
              'activation':  {'dtype': ['uint8', 'fp32'], 'algorithm': ['minmax'], 'scheme':['sym']},
              'weight': {'dtype': ['int8', 'fp32'], 'algorithm': ['kl']}
            },
            'conv2': {
              'activation':  {'dtype': ['fp32']},
              'weight': {'dtype': ['fp32']}
            }
          }
        tuning:
          accuracy_criterion:
            relative: 0.01
          objective: performance
          
        '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')

        framework_modelwise_capability = {
            'activation': {
                'dtype': ['uint8', 'fp32'],
                'scheme': ['asym', 'sym'],
                'granularity': ['per_tensor'],
                'algorithm': ['minmax', 'kl']
            },
            'weight': {
                'dtype': ['int8', 'fp32'],
                'scheme': [
                    'sym',
                ],
                'granularity': ['per_channel', 'per_tensor'],
                'algorithm': ['minmax']
            },
        }

        config.modelwise_tune_space(framework_modelwise_capability)

        framework_opwise_capability = {
            ('conv1', 'CONV2D'): {
                'activation': {
                    'dtype': ['uint8', 'fp32'],
                    'scheme': ['asym', 'sym'],
                    'granularity': ['per_tensor'],
                    'algorithm': ['minmax', 'kl']
                },
                'weight': {
                    'dtype': ['int8', 'fp32'],
                    'scheme': [
                        'sym',
                    ],
                    'granularity': ['per_channel', 'per_tensor'],
                    'algorithm': ['minmax']
                }},
            ('conv2', 'CONV2D'): {
                'activation': {
                    'dtype': ['uint8', 'fp32'],
                    'scheme': ['asym', 'sym'],
                    'granularity': ['per_tensor'],
                    'algorithm': ['minmax', 'kl']
                },
                'weight': {
                    'dtype': ['int8', 'fp32'],
                    'scheme': [
                        'sym',
                    ],
                    'granularity': ['per_channel', 'per_tensor'],
                    'algorithm': ['minmax']
                }},
        }

        tune_space = config.opwise_tune_space(framework_opwise_capability)
        self.assertEqual(tune_space[('conv1', 'CONV2D')]['weight']['algorithm'], ['minmax'])
        self.assertEqual(tune_space[('conv2', 'CONV2D')]['activation']['dtype'], ['fp32'])

    def test_prune(self):
        test = '''
        model:
          name: prune_yaml 
          framework: pytorch

        device: cpu

        pruning:
          magnitude:
            prune1:
              weights: ['layer1.0.conv1.weight',  'layer1.0.conv2.weight']
              target_sparsity: 0.3
              end_epoch: 1
            prune2:
              weights: ['layer1.0.conv3.weight', 'layer1.0.conv4.weight']
              target_sparsity: 0.2
          start_epoch: 0
          end_epoch: 20
          frequency: 2
          init_sparsity: 0.0
          target_sparsity: 0.5
      '''
        helper(test)
        config = conf.Conf('fake_conf.yaml')


if __name__ == "__main__":
    unittest.main()

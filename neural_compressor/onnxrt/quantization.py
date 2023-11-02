# Copyright (c) 2023 Intel Corporation
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

from enum import Enum

import onnxruntime as ort


from neural_compressor.common.tune.sampler import BaseSampler
from neural_compressor.common.tune.search_space import HyperParams
from neural_compressor.onnxrt.utility import FAKE_EVAL_RESULT, FakeModel
from neural_compressor.common import logger


class OptimizationLevel(Enum):
    """Optimization level for ORT graph."""

    DISABLED = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
    BASIC = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
    EXTENDED = ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
    ALL = ort.GraphOptimizationLevel.ORT_ENABLE_ALL


class ORTQuantizer:
    def __init__(
        self,
        fp32_model,
        calib_dataloader,
        quant_config,
        tuning_criterion=None,
        accuracy_criterion=None,
        eval_func=None,
        **kwargs
    ) -> None:
        self.fp32_model = fp32_model
        self.calib_dataloader = calib_dataloader
        self.quant_config = quant_config
        self.tuning_criterion = tuning_criterion
        self.accuracy_criterion = accuracy_criterion
        self._eval_func = eval_func

    def need_tuning(self) -> bool:
        """Whether the quantizer needs tuning."""
        return (
            (self.tuning_criterion is not None)
            and (self.accuracy_criterion is not None)
            and (self._eval_func is not None)
        )

    def _parse_user_config_into_q_config(self, quant_config):
        return quant_config

    def quantize(self):
        if self.need_tuning():
            return self.tuning()
        else:
            return self.internal_quantize(q_config=self._parse_user_config_into_q_config(self.quant_config))

    def internal_quantize(self, q_config):
        logger.info("Quantizing model with config: {}".format(q_config))
        return FakeModel()

    def evaluate(self, model) -> float:
        """Evaluate the model and return the accuracy."""
        logger.info("Evaluating model: {}".format(model))
        self._eval_func(model)
        return FAKE_EVAL_RESULT

    def report_result(self, model):
        """Evaluate the current model and report the result to tuner.

        Args:
            model: the quantized model or fp32 model.
        """
        pass

    def tuning(self):
        """Try to find the best quantization config and return the corresponding model.

        Steps:
            1. Initialize a tuner.
            2. Register a set of custom samplers(search space).
            3. Traverse the search space and return the best model.

        Returns:
            Return best model if found, otherwise return None.
        """
        tuner = self.init_tuner()
        self.register_custom_samplers(tuner)
        best_model = tuner.traverse(self)
        return best_model

    def init_tuner(self):
        from neural_compressor.common.tune import Tuner

        tuner = Tuner(
            baseline_model=self.fp32_model,
            accuracy_criterion=self.accuracy_criterion,
            tuning_criterion=self.tuning_criterion,
            eval_func=self._eval_func,
        )
        return tuner

    def register_custom_samplers(self, tuner) -> None:
        """Register a set of custom passes.

        Args:
            tuner (Tuner): The tuner to register custom passes.
        """
        ############################################
        # add graph optimization level sampler
        ############################################
        opt_level_hp = HyperParams(
            name="ort_graph_opt_level",
            params_space={
                "ort_graph_opt_level": [
                    OptimizationLevel.DISABLED,
                    OptimizationLevel.BASIC,
                    OptimizationLevel.EXTENDED,
                    OptimizationLevel.ALL,
                ]
            },
        )
        opt_level_sampler = BaseSampler(
            hp=opt_level_hp, name="ort_graph_opt_level", priority=optimization_level_sampler_config.priority
        )
        tuner.add_sampler(opt_level_sampler)

        ############################################
        # add sq sampler
        ############################################
        # assume the sq alpha is a list of float
        sq_alpha = smooth_quant_sampler_config.alpha
        sq_hp = HyperParams(name="sq_alpha", params_space={"alpha": sq_alpha})
        sq_sampler = BaseSampler(hp=sq_hp, name="sq_alpha", priority=smooth_quant_sampler_config.priority)
        tuner.add_sampler(sq_sampler)


def quantize(
    fp32_model,
    quant_config,
    calib_dataloader=None,
    calib_func=None,
    eval_func=None,
    eval_metric=None,
    tuning_criterion=None,
    accuracy_criterion=None,
    **kwargs
):
    """The main entrance for user to quantize model.

    Args:
        fp32_model: _description_
        quant_config: _description_
        calib_dataloader: _description_. Defaults to None.
        calib_func: _description_. Defaults to None.
        eval_func: _description_. Defaults to None.
        eval_metric: _description_. Defaults to None.
        tuning_criterion: _description_. Defaults to None.
        accuracy_criterion: _description_. Defaults to None.

    Returns:
        Quantized model.
    """
    quantizer = ORTQuantizer(
        fp32_model=fp32_model,
        calib_dataloader=calib_dataloader,
        quant_config=quant_config,
        tuning_criterion=tuning_criterion,
        accuracy_criterion=accuracy_criterion,
        eval_func=eval_func,
        **kwargs
    )
    return quantizer.quantize()

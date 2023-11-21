import torch
import copy
import os
from neural_compressor.torch.quantization.utils import set_module
from ..modules import BatchMatmul, Matmul, Autocast
from .modules import FP8Linear, FP8BatchMatmul, FP8Matmul, FP8Cast


quantization_mapping = {
    torch.nn.Linear: FP8Linear,
    BatchMatmul: FP8BatchMatmul,
    Matmul: FP8Matmul,
    Autocast: FP8Cast,
}
white_list = tuple(quantization_mapping.keys())


# without scale factor 0.9, the output will be abnormal.
E4M3_AMAX = torch.tensor(240*0.9, dtype=torch.float).to('hpu')
E5M2_AMAX = torch.tensor(57344*0.9, dtype=torch.float).to('hpu')


def quantize_dynamic(model, dtype=torch.float8_e4m3fn, inplace=True):

    from neural_compressor.torch.quantization.fp8.modules import (
        FP8DynamicLinear, 
        FP8DynamicMatmul,
        FP8DynamicBatchMatmul,
    )
    q_model = model if inplace else copy.deepcopy(model)
    from neural_compressor.torch.quantization.fp8.modules import FP8DynamicLinear
    for n, m in q_model.named_modules():
        if isinstance(m, torch.nn.Linear):
            new_m = FP8DynamicLinear(m, dtype) # need m for init
            set_module(q_model, n, new_m)
        elif isinstance(m, Matmul):
            new_m = FP8DynamicMatmul(dtype)
            set_module(q_model, n, new_m)
        elif isinstance(m, Matmul):
            new_m = FP8DynamicBatchMatmul(dtype)
            set_module(q_model, n, new_m)
        elif isinstance(m, Autocast):
            new_m = FP8Cast(dtype=dtype)
            set_module(q_model, n, new_m)
    return q_model


def _add_observer(model, qconfig):
    algorithm = qconfig.act_algo
    def input_observer_forward_pre_hook(self, input):
        try:
            if isinstance(input[0], torch.Tensor):
                self.input_activation_post_process(input[0])
            if hasattr(self, 'input_activation_post_process1') and isinstance(input[1], torch.Tensor):
                self.input_activation_post_process1(input[1])
            return input
        except Exception as e:
            # The KL algorithm may encounter a overflow error on EltwiseAdd.
            pass
    ### Insert input observer into model, only for fp8_e4m3 static quantization ###
    from .observer import MinMaxObserver, FP8HistogramObserver
    for name, module in model.named_modules():
        if isinstance(module, white_list):
            module.add_module(
                'input_activation_post_process', FP8HistogramObserver() if \
                            algorithm == 'kl' else MinMaxObserver()
            )
        if isinstance(module, (BatchMatmul, Matmul)):
            module.add_module(
                'input_activation_post_process1', FP8HistogramObserver() if \
                        algorithm == 'kl' else MinMaxObserver()
            )
        module.register_forward_pre_hook(input_observer_forward_pre_hook)


def prepare(model, qconfig):
    _add_observer(model, qconfig)
    return model

def _remove_observer(model, qconfig):
    for name, module in model.named_modules():
        HF_max = E4M3_AMAX if qconfig.dtype == torch.float8_e4m3fn else E5M2_AMAX
        if hasattr(module, 'input_activation_post_process'):
            if hasattr(module.input_activation_post_process, '_non_linear_param_search'):  # kl
                min_val, max_val = module.input_activation_post_process._non_linear_param_search()
            else:
                min_val = module.input_activation_post_process.min_val
                max_val = module.input_activation_post_process.max_val
            amax = torch.max(torch.abs(max_val), torch.abs(min_val))
            scale = HF_max / amax
            module.register_parameter('scale', torch.nn.Parameter(scale))
            delattr(module, 'input_activation_post_process')
        if hasattr(module, 'input_activation_post_process1'):
            if hasattr(module.input_activation_post_process1, '_non_linear_param_search'):
                min_val, max_val = module.input_activation_post_process1._non_linear_param_search()
            else:
                min_val = module.input_activation_post_process1.min_val
                max_val = module.input_activation_post_process1.max_val
            amax = torch.max(torch.abs(max_val), torch.abs(min_val))
            scale = HF_max / amax
            module.register_parameter('scale1', torch.nn.Parameter(scale))
            delattr(module, 'input_activation_post_process1')

        # remove observer hooks
        hook_map = module._forward_pre_hooks
        handle_ids_to_remove = set()
        for handle_id, hook_fn in hook_map.items():
            if hasattr(hook_fn, '__name__') and \
                hook_fn.__name__ == 'input_observer_forward_pre_hook':
                handle_ids_to_remove.add(handle_id)
        for handle_id in handle_ids_to_remove:
            hook_map.pop(handle_id)


def _replace_module(model, qconfig):
    for name, module in model.named_modules():
        if isinstance(module, white_list):
            QModule = quantization_mapping[type(module)]
            module = QModule(module, qconfig.dtype)
            set_module(model, name, module)


def convert(model, qconfig):
    _remove_observer(model, qconfig)
    _replace_module(model, qconfig)
    return model


def quantize(model, qconfig, calib_func, inplace=True):
    q_model = model if inplace else copy.deepcopy(model)
    q_model = prepare(q_model, qconfig)
    calib_func(q_model)
    q_model = convert(q_model, qconfig)
    return q_model


# def autotune(fp32_model, quant_config, tune_config, eval_func, ...):
#     pass
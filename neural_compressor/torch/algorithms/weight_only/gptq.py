#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (c) 2024 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import gc
import math
import random
import re
import time
from collections import UserDict, defaultdict
from functools import partial

import torch
import torch.nn as nn
from tqdm import tqdm

from neural_compressor.torch.utils import get_accelerator, is_transformers_imported, logger, set_module
from neural_compressor.torch.utils.auto_accelerator import auto_detect_accelerator

from .modules import WeightOnlyLinear

if is_transformers_imported():
    import transformers

    SUPPORTED_LAYERS = [nn.Conv2d, nn.Conv1d, nn.Linear, transformers.Conv1D]
else:
    SUPPORTED_LAYERS = [nn.Conv2d, nn.Conv1d, nn.Linear]
DEBUG = False
accelerator = auto_detect_accelerator()


# ==============model structure related==============
def is_leaf(module):
    """Judge whether a module has no child-modules.

    Args:
        module: torch.nn.Module

    Returns:
        a bool: whether a module has no child-modules.
    """
    children_cnt = 0
    for n in module.children():
        children_cnt += 1
    return True if children_cnt == 0 else False


def trace_gptq_target_blocks(module, module_types=[torch.nn.ModuleList, torch.nn.Sequential]):
    """Search transformer stacked structures, which is critical in LLMs and GPTQ execution.

    Args:
        module: torch.nn.Module
        module_types: List of torch.nn.Module.

    Returns:
        gptq_related_blocks = {
            "embeddings": {}, # Dict embedding layers before transformer stack module,
            "transformers_pre": {}, # TODO
            "transformers_name": string. LLMs' transformer stack module name ,
            "transformers": torch.nn.ModuleList. LLMs' transformer stack module,
            "transformers": {}, Dict# TODO
        }
    """
    if type(module).__name__ == "MixFormerSequentialForCausalLM":  # pragma: no cover
        gptq_related_blocks = {
            "embeddings": {},
            "transformers_pre": {},  # todo
            "transformers_name": "",  # None
            "transformers": [],  # None
            "transformers_post": {},  # todo
        }
        for n, m in module.named_modules():
            if type(m) in module_types:
                gptq_related_blocks["transformers_name"] = n
                gptq_related_blocks["transformers"] = m
                break
            else:
                continue
        for n, m in gptq_related_blocks["transformers"][0].named_modules():
            if is_leaf(m):
                gptq_related_blocks["embeddings"][n] = m
        gptq_related_blocks["transformers"] = gptq_related_blocks["transformers"][1:-1]
    else:
        gptq_related_blocks = {
            "embeddings": {},
            "transformers_pre": {},  # todo
            "transformers_name": "",  # None
            "transformers": [],  # None
            "transformers_post": {},  # todo
        }
        for n, m in module.named_modules():
            if type(m) in module_types:
                gptq_related_blocks["transformers_name"] = n
                gptq_related_blocks["transformers"] = m
                return gptq_related_blocks
            else:
                if is_leaf(m):
                    gptq_related_blocks["embeddings"][n] = m
    return gptq_related_blocks


def find_layers(module, layers=SUPPORTED_LAYERS, name=""):
    """Get all layers with target types."""
    if type(module) in layers:
        return {name: module}
    else:
        # use string type to find name:
        if isinstance(module, tuple(layers)):
            return {name: module}
        else:
            pass
    res = {}
    for name1, child in module.named_children():
        res.update(find_layers(child, layers=layers, name=name + "." + name1 if name != "" else name1))
    return res


def find_layers_name(module, layers=SUPPORTED_LAYERS, name=""):
    """Get all layers with target types."""
    if type(module) in layers:
        return [name]
    res = []
    for name1, child in module.named_children():
        res += find_layers_name(child, layers=layers, name=name + "." + name1 if name != "" else name1)
    return res


def log_quantizable_layers_per_transformer(transformer_blocks, layers=SUPPORTED_LAYERS):
    """Print all layers which will be quantized in GPTQ algorithm."""
    logger.info("* * Layer to be quantized * *")

    for block_id in range(len(transformer_blocks["transformers"])):
        transformer_block = transformer_blocks["transformers"][block_id]
        layers_for_this_tblock = find_layers_name(transformer_block)
        layer_names = [
            (transformer_blocks["transformers_name"] + "." + str(block_id) + "." + layer_name)
            for layer_name in layers_for_this_tblock
        ]
        for name in layer_names:
            logger.info(name)


class RAWGPTQuantizer(object):
    """Main API for GPTQ algorithm.

    Please refer to:
    GPTQ: Accurate Post-training Compression for Generative Pretrained Transformers
    url: https://arxiv.org/abs/2210.17323
    """

    def __init__(
        self,
        model,
        weight_config={},
        nsamples=128,
        use_max_length=True,
        max_seq_length=2048,
        device=None,
        use_layer_wise=False,
        model_path="",
        dataloader=None,
        *args,
        **kwargs,
    ):
        """
        Args:
            model: the fp32 model to quantize
            weight_config (dict, optional): contains all info required by GPTQ. Defaults to {}. For example,
            weight_config={
                'layer1':
                {
                    'bits': 4,
                    'group_size': 32,
                    'sym': False,
                    'percdamp': .01,
                    'act_order': False
                }
                ...
            }
            dataloader: an iterable containing calibration datasets, contains (inputs, targets)
            use_layer_wise (bool): Enables quantize model per layer. Defaults to False.
            model_path (str): Model path that is used to load state_dict per layer.
            device: cpu or cuda
        """
        # model
        self.model = model
        # self.use_cache = self.model.config.use_cache
        self.gptq_related_blocks = trace_gptq_target_blocks(self.model)  # get the transformer block list above
        self.dtype = next(iter(self.model.parameters())).dtype
        log_quantizable_layers_per_transformer(self.gptq_related_blocks)

        # weight config
        self.weight_config = weight_config
        # default settings, check configs
        self.dtype_default = "int"
        self.bits_default = 4
        self.group_size_default = 128
        self.block_size_default = 128
        self.percdamp_default = 0.01
        self.sym_default = False
        self.act_order_default = False
        self.static_groups_default = False
        self.perchannel_default = True
        self.mse_default = False
        self.use_double_quant_default = False
        self.double_quant_dtype_default = "int"
        self.double_quant_bits_default = 4
        self.double_quant_group_size_default = 128
        self.double_quant_sym_default = False
        self.check_layer_config()

        # device
        self.device = get_accelerator(kwargs.pop("device", "auto")).current_device_name()
        if not use_layer_wise:
            self.model.to(self.device)
        self.is_ready = False

        self.use_layer_wise = use_layer_wise
        if use_layer_wise:
            self.prepare_layer_wise(model_path)

        # dataloader
        self.use_max_length = use_max_length
        self.max_seq_length = max_seq_length
        self.dataloader_original = dataloader
        self.dataloader = []
        self.nsamples = nsamples

    def prepare_layer_wise(self, model_path):
        from neural_compressor.torch.algorithms.layer_wise import LWQ_WORKSPACE, get_path, register_weight_hooks
        import os
        os.makedirs(LWQ_WORKSPACE, exist_ok=True)
        if model_path == "":
            model_path = self.model.path
        assert model_path, "model_path should not be None."
        self.model_path = get_path(model_path)
        register_weight_hooks(
            self.model, self.model_path, device=self.device, clean_weight=True, saved_path=LWQ_WORKSPACE
        )
    
    def get_full_layer_name(self, sub_layer_name, block_idx):
        transformer_name = self.gptq_related_blocks["transformers_name"]
        return ".".join([transformer_name, str(block_idx), sub_layer_name])

    def check_layer_config(self):
        """Copy arguments from weight_config to built-in attributes."""

        for layer_name, config in self.weight_config.items():
            self.weight_config[layer_name]["dtype"] = config.get("dtype", self.dtype_default)
            self.weight_config[layer_name]["bits"] = config.get("bits", self.bits_default)
            self.weight_config[layer_name]["group_size"] = config.get("group_size", self.group_size_default)
            self.weight_config[layer_name]["block_size"] = config.get("block_size", self.group_size_default)
            self.weight_config[layer_name]["percdamp"] = config.get("percdamp", self.percdamp_default)
            self.weight_config[layer_name]["sym"] = config.get("sym", self.sym_default)
            self.weight_config[layer_name]["act_order"] = config.get("act_order", self.act_order_default)
            self.weight_config[layer_name]["static_groups"] = config.get("static_groups", self.static_groups_default)
            self.weight_config[layer_name]["perchannel"] = config.get("perchannel", self.perchannel_default)
            self.weight_config[layer_name]["mse"] = config.get("mse", self.mse_default)
            self.weight_config[layer_name]["use_double_quant"] = config.get(
                "use_double_quant", self.use_double_quant_default
            )
            self.weight_config[layer_name]["double_quant_dtype"] = config.get(
                "double_quant_dtype", self.double_quant_dtype_default
            )  # only support int
            self.weight_config[layer_name]["double_quant_bits"] = config.get(
                "double_quant_bits", self.double_quant_bits_default
            )
            self.weight_config[layer_name]["double_quant_group_size"] = config.get(
                "double_quant_group_size", self.double_quant_group_size_default
            )
            self.weight_config[layer_name]["double_quant_sym"] = config.get(
                "double_quant_sym", self.double_quant_sym_default
            )
            if self.weight_config[layer_name]["dtype"] != "int" and "int" in self.weight_config[layer_name]["dtype"]:
                self.weight_config[layer_name]["bits"] = int(self.weight_config[layer_name]["dtype"].lstrip("int"))
                self.weight_config[layer_name]["dtype"] = "int"

    def get_layer_config(self, layer_name):
        """Obtain config for one layer, since GPTQ supports layer-wise config."""
        # First try the exact name matching, if cannot find, use re to search. For example, can support ".*" in op_name
        config = None
        config = self.weight_config.get(layer_name, None)
        if config is not None:
            return config
        else:
            for k, v in self.weight_config.items():
                regex = re.compile(k)
                if len(regex.findall(layer_name)) is not None:
                    config = v
                    return config
                else:
                    pass
        return config

    def track_hidden_states(self, data):
        if isinstance(data, torch.Tensor):
            return data
        elif isinstance(data, tuple) or isinstance(data, list):
            return data[0]

    @torch.no_grad()
    def prepare_for_calibration(self):
        """Prepare input calibration data and other attributes which are critical for gptq execution."""
        try:
            self.cache_key_arguments = {
                "batch_num": 0
            }  # a dict of list, keyword arguments ("attention_masks", "position_ids", etc.)
            # Note that the first elements in cache_positional_arguments is main input: hidden_states
            self.cache_positional_arguments = []  # a list of list, positional arguments ("rotary_pos_emb" in chatglm)
            self.is_ready = True
        except:
            logger.warning("GPTQ Quantizer initialization failed!")
            pass

        # critical: hooker function which collects inputs
        def forward(layer, *args, **kwargs):
            # inputs[inputs_info['idx']] = input_ids # TODO solve the problem of batchsize!=1
            self.cache_key_arguments["batch_num"] += 1
            for arg in kwargs:
                # TODO: investigate include parameters
                # each outputs can be different shape, hence also use list to store
                if isinstance(kwargs[arg], torch.Tensor) or arg == "alibi":
                    if self.cache_key_arguments.get(arg, None) is None:
                        self.cache_key_arguments[arg] = []
                    self.cache_key_arguments[arg].append(kwargs[arg])
                continue
            # copy positional arguments, positional arguments are sensitive for their order, be cautious!
            # Most models in HF has avoid this, but some models still use positional arguments other than
            # hidden_states, chatglm2-6b etc.
            for idx, item in enumerate(args):
                if (idx + 1) > len(self.cache_positional_arguments):
                    # initialize
                    self.cache_positional_arguments.append([])
                self.cache_positional_arguments[idx].append(item)
            raise ValueError

        # Step1: fetch the embeddings and other layers before the transformer stack.
        if not self.use_layer_wise:  # pragma: no cover
            for embedding_name, embedding_layer in self.gptq_related_blocks["embeddings"].items():
                embedding_layer = embedding_layer.to(self.device)

        # Step2: modify the first transformer block's forward function to obtain inputs for calibration
        if not self.use_layer_wise:  # pragma: no cover
            self.gptq_related_blocks["transformers"][0] = self.gptq_related_blocks["transformers"][0].to(self.device)
        self.forward_cache = self.gptq_related_blocks["transformers"][0].forward
        self.gptq_related_blocks["transformers"][0].forward = partial(
            forward, self.gptq_related_blocks["transformers"][0]
        )

    @torch.no_grad()
    def remove_prepare_for_calibration(self):
        # output inp data shape
        logger.info("All calibration data's shape =>")
        # check all hidden_states shape
        try:
            for hidden_states in self.cache_positional_arguments[0]:
                logger.info(hidden_states.shape)
        except:
            pass
        logger.info("Done.")

        # Step 4: restore original forward function, relocate layers back to cpu.
        self.gptq_related_blocks["transformers"][0].forward = self.forward_cache
        if not self.use_layer_wise:  # pragma: no cover
            self.gptq_related_blocks["transformers"][0] = self.gptq_related_blocks["transformers"][0].cpu()
            for embedding_name, embedding_layer in self.gptq_related_blocks["embeddings"].items():
                embedding_layer.to(self.device)
        torch.cuda.empty_cache()
        # end
        logger.info("GPTQ quantization prepared.")

    def gather_single_batch_from_dict(self, data_dict, idx):
        # obtain a set of keyword input from cache
        single_batch = {}
        for k, v in data_dict.items():
            single_batch[k] = data_dict[k][idx]
        return single_batch

    def gather_single_batch_from_list(self, data_list, idx):
        # obtain a set of keyword input from cache
        single_batch = []
        for data_item in data_list:
            single_batch.append(data_item[idx])
        return single_batch

    def update_blockwise_hidden_states(self, outs):
        if "hidden_states" in self.cache_key_arguments:
            self.cache_key_arguments["hidden_states"] = outs[:]
        else:
            self.cache_positional_arguments[0] = outs[:]

    @torch.no_grad()
    def execute_quantization(self, means=None, stds=None):
        """Run quantization."""
        # Step1: prepare quantization (calibration datasets)

        logger.info("Begin ====>")

        # Step2: run gptq quantization in a transformer block-wise manner.
        gptq_config = {}
        tblock_length = len(self.gptq_related_blocks["transformers"])
        for block_idx in range(tblock_length):
            logger.info(f"Quantizing layer {block_idx + 1} / {tblock_length}..")
            if not self.use_layer_wise:  # pragma: no cover
                # if we do not apply layer-wise feature, we still place the entire block on the GPU
                transformer_block = self.gptq_related_blocks["transformers"][block_idx].to(self.device)
            else:
                transformer_block = self.gptq_related_blocks["transformers"][block_idx]  # .to(self.device)
            # Step2.1: obtain all layers (Linear, Conv2d, etc) in the block which can be quantized.
            sub_layers = find_layers(transformer_block)
            sub_layers_to_quant = {}
            for layer_name, layer_obj in sub_layers.items():
                # filter sub_layers with included layer_names in self.weight_config
                full_layer_name = self.get_full_layer_name(layer_name, block_idx)
                # if self.weight_config.get(full_layer_name, None) == None:
                if self.get_layer_config(full_layer_name) is None:
                    logger.warning(f"{full_layer_name} can be quantized " + "but excluded from quantization configs.")
                else:
                    sub_layers_to_quant[layer_name] = layer_obj
            del sub_layers
            sub_layers = sub_layers_to_quant
            # Step 2.2: Initialize GPTQ quantizers for collected layers.
            gptq_for_this_block = {}
            # initialize gptq quantizer for every layer in a transformer block
            for layer_name in sub_layers:
                # weight_config_this_layer = self.weight_config.get(
                #     self.get_full_layer_name(layer_name, block_idx), None
                # )
                full_layer_name = self.get_full_layer_name(layer_name, block_idx)
                weight_config_this_layer = self.get_layer_config(full_layer_name)
                if self.use_layer_wise:  # pragma: no cover
                    from neural_compressor.torch.algorithms.layer_wise import load_value
                    # import pdb; pdb.set_trace()
                    W = load_value(self.model, full_layer_name + ".weight", self.model_path)
                else:
                    W = sub_layers[layer_name].weight.data.clone()

                gptq_for_this_block[layer_name] = GPTQ(sub_layers[layer_name], W, self.device)
                # gptq_for_this_block[layer_name].quantizer = Quantizer()
                gptq_for_this_block[layer_name].quantizer.configure(weight_config_this_layer)

            # Step 2.3: modify forward functions to hook inputs data (used in gptq execution)
            def add_batch(_name):
                def tmp(_, inp, out):
                    gptq_for_this_block[_name].add_batch(inp[0].data, out.data)  # noqa: F821

                return tmp

            handles = []  # register handles which add inputs and outputs to gptq object
            for layer_name in sub_layers:
                handles.append(sub_layers[layer_name].register_forward_hook(add_batch(layer_name)))
            batch_num = self.cache_key_arguments.pop("batch_num")
            for j in range(batch_num):
                cache_keyword_batch = self.gather_single_batch_from_dict(self.cache_key_arguments, j)
                cache_positional_batch = self.gather_single_batch_from_list(self.cache_positional_arguments, j)
                accelerator.mark_step()
                out = transformer_block(*cache_positional_batch, **cache_keyword_batch)
                out = self.track_hidden_states(out)
            self.cache_key_arguments["batch_num"] = batch_num
            for h in handles:
                h.remove()
            # Step 2.4: everything is prepared, so start quantization!
            for layer_name in sub_layers:
                # weight_config_this_layer = self.weight_config.get(
                #     self.get_full_layer_name(layer_name, block_idx), None
                # )
                weight_config_this_layer = self.get_layer_config(self.get_full_layer_name(layer_name, block_idx))
                logger.info(f"Quantizing layer {layer_name}")
                if self.use_layer_wise:  # pragma: no cover
                    from neural_compressor.torch.algorithms.layer_wise import load_value, set_module_tensor_to_device

                    full_layer_name = self.get_full_layer_name(layer_name, block_idx)
                    for n, p in sub_layers[layer_name].named_parameters():
                        param_name = full_layer_name + "." + n
                        # breakpoint()
                        if n == "weight":
                            W = load_value(self.model, full_layer_name + ".weight", self.model_path)
                        else:
                            value = load_value(self.model, param_name, self.model_path)
                            set_module_tensor_to_device(self.model, param_name, self.device, value)
                    
                else:
                    W = sub_layers[layer_name].weight.data.clone()
                    
                    
                    
                accelerator.mark_step()
                if "hpu" in self.device:
                    W = W.to("cpu")
                scale, zp, Q = gptq_for_this_block[layer_name].fasterquant(
                    W,
                    blocksize=weight_config_this_layer["block_size"],
                    percdamp=weight_config_this_layer["percdamp"],
                    groupsize=weight_config_this_layer["group_size"],
                    act_order=weight_config_this_layer["act_order"],
                    static_groups=weight_config_this_layer["static_groups"],
                )
                
                # Step 2.5: export to compressed model
                gptq_config[self.get_full_layer_name(layer_name, block_idx)] = {"scale": scale}
                if not weight_config_this_layer["sym"]:
                    gptq_config[self.get_full_layer_name(layer_name, block_idx)]["zero"] = zp
                if weight_config_this_layer["act_order"]:  # save perm for restoring the weights
                    gptq_config[self.get_full_layer_name(layer_name, block_idx)]["perm"] = gptq_for_this_block[
                        layer_name
                    ].perm
                
                weight_config_this_layer = self.get_layer_config(self.get_full_layer_name(layer_name, block_idx))
                gptq_scale = gptq_config[self.get_full_layer_name(layer_name, block_idx)]["scale"]
                if not weight_config_this_layer["sym"]:
                    gptq_zp = gptq_config[self.get_full_layer_name(layer_name, block_idx)]["zero"]
                else:
                    gptq_zp = None
                if weight_config_this_layer["act_order"]:  # save perm for restoring the weights
                    gptq_perm = gptq_config[self.get_full_layer_name(layer_name, block_idx)]["perm"]
                else:
                    gptq_perm = None
                if weight_config_this_layer["act_order"]:
                    Q.copy_(Q[:, gptq_perm])
                if is_transformers_imported() and isinstance(sub_layers[layer_name], transformers.Conv1D):
                    Q = Q.t_().contiguous()
                from .utility import quant_weight_w_scale

                quant_weight_w_scale(
                    Q,
                    gptq_scale,
                    gptq_zp,
                    weight_config_this_layer["group_size"],
                    dtype=weight_config_this_layer["dtype"],
                )
                if weight_config_this_layer["act_order"]:
                    invperm = torch.argsort(gptq_perm)
                    Q.copy_(Q[:, invperm])
                int_weight = Q.type(torch.int32)  # copy_ is not workable for different types.
                # replace module
                if isinstance(sub_layers[layer_name], torch.nn.Linear):
                    in_features = sub_layers[layer_name].in_features
                    out_features = sub_layers[layer_name].out_features
                elif is_transformers_imported() and isinstance(sub_layers[layer_name], transformers.Conv1D):
                    in_features = sub_layers[layer_name].weight.shape[0]
                    out_features = sub_layers[layer_name].weight.shape[1]
                    int_weight = sub_layers[layer_name].weight.t_().contiguous()
                    scale = scale.t_().contiguous()
                    zp = zp.t_().contiguous() if zp is not None else zp

                new_module = WeightOnlyLinear(
                    in_features,
                    out_features,
                    dtype=weight_config_this_layer["dtype"],
                    bits=weight_config_this_layer["bits"],
                    group_size=weight_config_this_layer["group_size"],
                    zp=gptq_zp is not None,
                    bias=sub_layers[layer_name].bias is not None,
                    g_idx=gptq_perm is not None,
                    device=self.device,
                )
                new_module.pack(int_weight, gptq_scale, gptq_zp, sub_layers[layer_name].bias, gptq_perm)
                
                    
                if self.use_layer_wise:  # pragma: no cover
                    from neural_compressor.torch.algorithms.layer_wise import (
                        LWQ_WORKSPACE,
                        clean_module_weight,
                        load_value,
                        set_module_tensor_to_device,
                    )

                    # sub_layer = sub_layers[layer_name]
                    # full_layer_name = self.get_full_layer_name(layer_name, block_idx)
                    # for n, p in sub_layer.named_parameters():
                    #     param_name = full_layer_name + "." + n
                    #     # breakpoint()
                    #     if n == "weight":
                    #         set_module_tensor_to_device(self.model, param_name, self.device, Q)
                    #     else:
                    #         value = load_value(self.model, param_name, model_path)
                    #         set_module_tensor_to_device(self.model, param_name, self.device, value)
                    # sub_layer.weight.data = Q
                    # torch.save(sub_layer.state_dict(), LWQ_WORKSPACE + f"/{full_layer_name}.pt")
                    torch.save(new_module.state_dict(), LWQ_WORKSPACE + f"/{full_layer_name}.pt")
                    clean_module_weight(new_module)
                    del Q
                    gc.collect()
                set_module(transformer_block, layer_name, new_module)

                gptq_for_this_block[layer_name].free()

            # Step 2.6: replace output data with quantized weights
            outs = []
            batch_num = self.cache_key_arguments.pop("batch_num")
            for j in range(batch_num):
                cache_keyword_batch = self.gather_single_batch_from_dict(self.cache_key_arguments, j)
                cache_positional_batch = self.gather_single_batch_from_list(self.cache_positional_arguments, j)
                out = transformer_block(*cache_positional_batch, **cache_keyword_batch)
                out = self.track_hidden_states(out)
                outs.append(out)
            self.cache_key_arguments["batch_num"] = batch_num
            if self.use_layer_wise:  # pragma: no cover
                self.gptq_related_blocks["transformers"][block_idx] = transformer_block
            else:
                self.gptq_related_blocks["transformers"][block_idx] = transformer_block.cpu()
            
                
            del gptq_for_this_block
            torch.cuda.empty_cache()
            # iteratively replace the input with output, thus layerwise quantization can continue.
            self.update_blockwise_hidden_states(outs)
            logger.info("------------------------------")

        logger.info("Quantization done")
        # self.model.config.use_cache = self.use_cache

        # obtain model (all weight only quantization API function should return)
        for k, v in gptq_config.items():
            for m, n in v.items():
                gptq_config[k][m] = n.tolist()
        return self.model, gptq_config


class GPTQ:
    """
    Please refer to:
    GPTQ: Accurate Post-training Compression for Generative Pretrained Transformers (https://arxiv.org/abs/2210.17323)
    """

    def __init__(self, layer, W, device="cpu"):
        self.layer = layer
        self.device = device
        # W = layer.weight.data.clone()
        if isinstance(self.layer, nn.Conv2d) or isinstance(self.layer, nn.Conv1d):
            W = W.flatten(1)
        if is_transformers_imported() and isinstance(self.layer, transformers.Conv1D):
            W = W.t()
        self.rows = W.shape[0]  # output channels
        self.columns = W.shape[1]  # input channels
        self.H = torch.zeros((self.columns, self.columns), device=self.device)
        self.nsamples = 0
        self.quantizer = Quantizer()
        self.perm = None  # act_order choice

    def add_batch(self, inp, out):
        # if DEBUG:
        #     self.inp1 = inp
        #     self.out1 = out
        if len(inp.shape) == 2:
            inp = inp.unsqueeze(0)
        tmp = inp.shape[0]
        if isinstance(self.layer, nn.Linear) or (
            is_transformers_imported() and isinstance(self.layer, transformers.Conv1D)
        ):
            if len(inp.shape) == 3:
                inp = inp.reshape((-1, inp.shape[-1]))
            inp = inp.t()
        # TODO: llm's transformer sequential with nn.conv2d is currently not under test
        # if isinstance(self.layer, nn.Conv2d):
        #     unfold = nn.Unfold(
        #         self.layer.kernel_size,
        #         dilation=self.layer.dilation,
        #         padding=self.layer.padding,
        #         stride=self.layer.stride
        #     )
        #     inp = unfold(inp)
        #     inp = inp.permute([1, 0, 2])
        #     inp = inp.flatten(1)
        self.H *= self.nsamples / (self.nsamples + tmp)
        self.nsamples += tmp
        # inp = inp.float()
        inp = math.sqrt(2 / self.nsamples) * inp.float()
        # self.H += 2 / self.nsamples * inp.matmul(inp.t())
        self.H += inp.matmul(inp.t())  # H = X*X, which should be a sym matrix

    def fasterquant(self, W, blocksize=128, percdamp=0.01, groupsize=-1, act_order=False, static_groups=False):
        # W = self.layer.weight.data.clone()
        weight_shape, weight_dtype = W.shape, W.data.dtype
        if isinstance(self.layer, nn.Conv2d):
            W = W.flatten(1)
        if is_transformers_imported() and isinstance(self.layer, transformers.Conv1D):
            W = W.t()
        W = W.float()

        tick = time.time()

        if not self.quantizer.ready():
            self.quantizer.find_params(W, weight=True)

        H = self.H
        if "hpu" in self.device:
            H = H.to("cpu")
        del self.H
        dead = torch.diag(H) == 0
        H[dead, dead] = 1
        W[:, dead] = 0  # such channel makes no contribution to quantization computation

        # enable static_groups
        # calculate the quantization parameters for original group in advance.
        if static_groups:
            import copy

            groups = []
            for i in range(0, self.columns, groupsize):
                quantizer = copy.deepcopy(self.quantizer)
                quantizer.find_params(W[:, i : (i + groupsize)], weight=True)
                groups.append(quantizer)

        # rearrange considering the diag's value
        if act_order:
            perm = torch.argsort(torch.diag(H), descending=True)
            W = W[:, perm]
            H = H[perm][:, perm]
            self.perm = perm.clone()

        Losses = torch.zeros_like(W)
        Q = torch.zeros_like(W)

        damp = percdamp * torch.mean(torch.diag(H))
        diag = torch.arange(self.columns, device=self.device)
        H[diag, diag] += damp  # add a average value of
        H = torch.linalg.cholesky(H)
        H = torch.cholesky_inverse(H)
        H = torch.linalg.cholesky(H, upper=True)
        Hinv = H

        scale = []
        zero = []

        for i1 in range(0, self.columns, blocksize):
            i2 = min(i1 + blocksize, self.columns)
            count = i2 - i1

            W1 = W[:, i1:i2].clone()
            Q1 = torch.zeros_like(W1)
            Err1 = torch.zeros_like(W1)
            Losses1 = torch.zeros_like(W1)
            Hinv1 = Hinv[i1:i2, i1:i2]

            for i in range(count):  # within a block, channel wise
                w = W1[:, i]
                d = Hinv1[i, i]

                if groupsize != -1:
                    if not static_groups:
                        if (i1 + i) % groupsize == 0:
                            self.quantizer.find_params(W[:, (i1 + i) : (i1 + i + groupsize)], weight=True)
                            scale.append(self.quantizer.scale)
                            zero.append(self.quantizer.zero)
                    else:
                        idx = i1 + i
                        if act_order:
                            idx = perm[idx]
                        self.quantizer = groups[idx // groupsize]
                q = self.quantizer.quantize(
                    w.unsqueeze(1), self.quantizer.scale, self.quantizer.zero, self.quantizer.maxq
                ).flatten()
                Q1[:, i] = q
                Losses1[:, i] = (w - q) ** 2 / d**2

                err1 = (w - q) / d
                W1[:, i:] -= err1.unsqueeze(1).matmul(Hinv1[i, i:].unsqueeze(0))
                Err1[:, i] = err1

            Q[:, i1:i2] = Q1
            Losses[:, i1:i2] = Losses1 / 2

            W[:, i2:] -= Err1.matmul(Hinv[i1:i2, i2:])

            # if DEBUG:
            #     self.layer.weight.data[:, :i2] = Q[:, :i2]
            #     self.layer.weight.data[:, i2:] = W[:, i2:]
            #     logger.info(f"{torch.sum((self.layer(self.inp1) - self.out1) ** 2)}")
            #     logger.info(f"{torch.sum(Losses)}")

        if str(self.device).startswith("cuda"):
            torch.cuda.synchronize()
        logger.info(f"time {(time.time() - tick)}")
        logger.info(f"error {torch.sum(Losses).item()}")

        if act_order:
            invperm = torch.argsort(perm)
            Q = Q[:, invperm]

        if is_transformers_imported() and isinstance(self.layer, transformers.Conv1D):
            Q = Q.t()
        # self.layer.weight.data = Q.reshape(self.layer.weight.shape).to(self.layer.weight.data.dtype)
        Q = Q.reshape(weight_shape).to(weight_dtype)
        if DEBUG:
            logger.info(f"{torch.sum((self.layer(self.inp1) - self.out1) ** 2)}")

        if scale == []:
            scale.append(self.quantizer.scale)
            zero.append(self.quantizer.zero)
        scale = torch.cat(scale, dim=1)
        zero = torch.cat(zero, dim=1)
        if "hpu" in self.device:
            scale = scale.to(self.device)
            zero = zero.to(self.device)
            Q = Q.to(self.device)
        return scale, zero, Q

    def free(self):
        if DEBUG:
            self.inp1 = None
            self.out1 = None
        self.H = None
        self.Losses = None
        self.Trace = None
        torch.cuda.empty_cache()


class Quantizer(nn.Module):
    def __init__(self, shape=1):
        super(Quantizer, self).__init__()
        self.maxq = 0
        self.register_buffer("scale", torch.zeros(shape))
        self.register_buffer("zero", torch.zeros(shape))

    def configure(self, weight_config_this_layer, norm=2.4, grid=100, maxshrink=0.8, trits=False):
        for k, v in weight_config_this_layer.items():
            setattr(self, k, v)
        # self.maxq = torch.tensor(2**self.bits - 1)
        self.maxq = 2**self.bits - 1
        self.scheme = "sym" if self.sym else "asym"
        self.double_quant_scheme = "sym" if self.double_quant_sym else "asym"
        self.norm = norm
        self.grid = grid
        self.maxshrink = maxshrink
        if trits:
            self.maxq = -1

    def find_params(self, x, weight=False):
        dev = x.device
        # NF4 FP4
        if self.dtype != "int":
            from .utility import quant_tensor

            tmp = x.clone()  # tmp will be replaced after quant_tensor
            _, scale, zero = quant_tensor(
                tmp,
                dtype=self.dtype,
                bits=self.bits,
                group_size=self.group_size,
                scheme=self.scheme,
                quantile=1.0,
                return_int=True,
                full_range=False,
                double_quant=self.use_double_quant,
                double_quant_dtype=self.double_quant_dtype,
                double_quant_bits=self.double_quant_bits,
                double_quant_scheme=self.double_quant_scheme,
                double_quant_group_size=self.double_quant_group_size,
                double_quant_return_int=False,
            )
            self.scale = scale
            self.zero = torch.zeros_like(scale)
            return
        # INT
        shape = x.shape
        if self.perchannel:
            if weight:
                x = x.flatten(1)
            else:
                if len(shape) == 4:
                    x = x.permute([1, 0, 2, 3])
                    x = x.flatten(1)
                if len(shape) == 3:
                    x = x.reshape((-1, shape[-1])).t()
                if len(shape) == 2:
                    x = x.t()
        else:
            x = x.flatten().unsqueeze(0)

        tmp = torch.zeros(x.shape[0], device=dev)
        xmin = torch.minimum(x.min(1)[0], tmp)
        xmax = torch.maximum(x.max(1)[0], tmp)

        if self.sym:
            xmax = torch.maximum(torch.abs(xmin), xmax)
            tmp = xmin < 0
            if torch.any(tmp):
                xmin[tmp] = -xmax[tmp]
        tmp = (xmin == 0) & (xmax == 0)
        xmin[tmp] = -1
        xmax[tmp] = +1

        if self.maxq < 0:
            self.scale = xmax
            self.zero = xmin
        else:
            self.scale = (xmax - xmin) / self.maxq
            if self.sym:
                self.zero = torch.full_like(self.scale, (self.maxq + 1) / 2)
            else:
                self.zero = torch.round(-xmin / self.scale)

        if self.mse:
            best = torch.full([x.shape[0]], float("inf"), device=dev)
            for i in range(int(self.maxshrink * self.grid)):
                p = 1 - i / self.grid
                xmin1 = p * xmin
                xmax1 = p * xmax
                scale1 = (xmax1 - xmin1) / self.maxq
                zero1 = torch.round(-xmin1 / scale1) if not self.sym else self.zero
                q = self.quantize(x, scale1.unsqueeze(1), zero1.unsqueeze(1), self.maxq)
                q -= x
                q.abs_()
                q.pow_(self.norm)
                err = torch.sum(q, 1)
                tmp = err < best
                if torch.any(tmp):
                    best[tmp] = err[tmp]
                    self.scale[tmp] = scale1[tmp]
                    self.zero[tmp] = zero1[tmp]
        if not self.perchannel:
            if weight:
                tmp = shape[0]
            else:
                tmp = shape[1] if len(shape) != 3 else shape[2]
            self.scale = self.scale.repeat(tmp)
            self.zero = self.zero.repeat(tmp)

        if weight:
            shape = [-1] + [1] * (len(shape) - 1)
            self.scale = self.scale.reshape(shape)
            self.zero = self.zero.reshape(shape)

            if self.use_double_quant:
                # for INT
                from .utility import quant_tensor

                orig_scale_shape = self.scale.shape
                self.scale = self.scale.reshape(1, -1)
                quant_tensor(
                    self.scale,
                    dtype=self.double_quant_dtype,
                    bits=self.double_quant_bits,
                    group_size=self.double_quant_group_size,
                    scheme=self.double_quant_scheme,
                    quantile=1.0,
                    return_int=False,
                    full_range=False,
                )
                self.scale = self.scale.reshape(orig_scale_shape)
            return
        if len(shape) == 4:
            self.scale = self.scale.reshape((1, -1, 1, 1))
            self.zero = self.zero.reshape((1, -1, 1, 1))
        if len(shape) == 3:
            self.scale = self.scale.reshape((1, 1, -1))
            self.zero = self.zero.reshape((1, 1, -1))
        if len(shape) == 2:
            self.scale = self.scale.unsqueeze(0)
            self.zero = self.zero.unsqueeze(0)

    def quantize(self, x, scale, zero, maxq):
        """Do quantization."""
        if self.dtype != "int":
            from .utility import quantize_4bit

            tmp = x.clone()  # tmp will be replaced after quant_tensor
            return quantize_4bit(tmp, dtype=self.dtype, scale=scale)
        else:
            if maxq < 0:
                return (x > scale / 2).float() * scale + (x < zero / 2).float() * zero
            q = torch.clamp(torch.round(x / scale) + zero, 0, maxq)
            return scale * (q - zero)

    def ready(self):
        return torch.all(self.scale != 0)


from neural_compressor.torch.algorithms import Quantizer as INCQuantizer


class GPTQuantizer(INCQuantizer):
    def __init__(self, quant_config={}):
        """Init a RTNQuantizer object.

        Args:
            quant_config (OrderedDict, optional): quantization config for ops. Defaults to {}.
        """
        super().__init__(quant_config)

    @torch.no_grad()
    def prepare(
        self,
        model,
        nsamples=128,
        max_seq_length=2048,
        use_max_length=True,
        device=None,
        use_layer_wise=False,
        model_path=None,
        *args,
        **kwargs,
    ):
        """Run weight-only quantization with."""
        # TODO: unify weight_config keys, add docstring, and support default config
        assert isinstance(model, torch.nn.Module), "only support torch module"
        if use_layer_wise:  # pragma: no cover
            assert model_path is not None, "model_path should not be None when use layer wise mode"

        self.gptq_quantizer = RAWGPTQuantizer(
            model,
            weight_config=self.quant_config,
            nsamples=nsamples,
            use_max_length=use_max_length,
            max_seq_length=max_seq_length,
            device=device,
            use_layer_wise=use_layer_wise,
            model_path=model_path,
        )
        self.gptq_quantizer.prepare_for_calibration()
        return self.gptq_quantizer.model

    @torch.no_grad()
    def convert(self, model, *args, **kwargs):
        self.gptq_quantizer.model = model
        self.gptq_quantizer.remove_prepare_for_calibration()

        q_model, gptq_config = self.gptq_quantizer.execute_quantization()
        q_model.gptq_config = gptq_config
        logger.info("GPTQ quantizing done.")
        return q_model

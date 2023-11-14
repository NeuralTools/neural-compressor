import os
import torch
import habana_frameworks.torch.hpex
from torch.nn import functional as F
from neural_compressor.common import logger


class FP8DynamicLinear(torch.nn.Module):
    def __init__(self, org_module, use_amax=True) -> None:
        super().__init__()
        # attributes
        org_module.to('hpu')
        self.use_amax = use_amax
        self.dtype = 'E4M3' if os.getenv('PT_USE_FP8_143') is not None else 'E5M2'
        self.dtype_amax = 240 if self.dtype == 'E4M3' else 57344
        self.in_features = org_module.in_features
        self.out_features = org_module.out_features
        self.weight_dtype = torch.int8
        self.out_dtype = torch.float32
        self.register_buffer(
            'weight', 
            torch.empty(
                self.out_features,
                self.in_features,
                device="hpu",
                dtype=self.weight_dtype,
            )
        )
        self.register_buffer(
            'bias', 
            torch.empty(
                self.out_features,
                device="hpu",
                dtype=self.out_dtype,
            ) 
        )
        # user configuration
        # scale = HF_max /amax
        if self.use_amax:
            self.weight_scale = self.dtype_amax / org_module.weight.data.abs().max()
            self.weight_scale_inv = 1.0 / self.weight_scale
        else:
            self.weight_scale = None
            self.weight_scale_inv = None
        self.weight = torch.ops.hpu.cast_to_fp8_v2(
            org_module.weight.data, self.weight_scale, False, False
        )[0]
        if org_module.bias is not None:
            self.bias = org_module.bias.data.type(self.out_dtype)
        else:
            self.bias = None

    def forward(self, inp):
        if self.use_amax:
            input_scale = self.dtype_amax / inp.abs().max()
            input_scale_inv = 1.0 / input_scale
        else:
            input_scale = None
            input_scale_inv = None
        logger.debug(f"dtype_amax: {self.dtype_amax}, use_amax: {self.use_amax}")
        assert inp.shape[-1] == self.in_features, "GEMM not possible"
        inputmat = inp.view((-1, self.in_features))
        inputmat = torch.ops.hpu.cast_to_fp8_v2(inputmat, input_scale, False, False)[0]
        out = torch.ops.hpu.fp8_gemm_v2(
            inputmat,
            False,
            self.weight,
            True,
            None,
            self.out_dtype,
            input_scale_inv, # inv is used for recover scale
            self.weight_scale_inv,
            self.bias,
            False,
        )
        return out.view(-1, *inp.shape[1:-1], out.shape[-1])

    def extra_repr(self) -> str:
        import os
        return 'in_features={}, out_features={}, bias={}, format={}'.format(
            self.in_features, self.out_features, self.bias is not None, self.dtype,
        )



class FP8Linear(torch.nn.Module):
    def __init__(self, org_module) -> None:
        super().__init__()
        # attributes
        org_module.to('hpu')
        self.dtype = 'E4M3' if os.getenv('PT_USE_FP8_143') is not None else 'E5M2'
        self.dtype_amax = 240 if self.dtype == 'E4M3' else 57344
        self.in_features = org_module.in_features
        self.out_features = org_module.out_features
        self.weight_dtype = torch.int8
        self.out_dtype = torch.float32
        self.register_buffer(
            'weight', 
            torch.empty(
                self.out_features,
                self.in_features,
                device="hpu",
                dtype=self.weight_dtype,
            )
        )
        self.register_buffer(
            'bias', 
            torch.empty(
                self.out_features,
                device="hpu",
                dtype=self.out_dtype,
            ) 
        )
        assert hasattr(org_module, "scale"), "scale is not recorded when convert to FP8Linear."
        self.register_buffer(
            'scale', 
            torch.tensor(
                org_module.scale,
                device="hpu",
                dtype=self.out_dtype,
            ) 
        )
        self.scale_inv = 1.0 / self.scale
        # user configuration
        # scale = HF_max /amax
        self.weight_scale = self.dtype_amax / org_module.weight.data.abs().max()
        self.weight_scale_inv = 1.0 / self.weight_scale
        self.weight = torch.ops.hpu.cast_to_fp8_v2(
            org_module.weight.data, self.weight_scale, False, False
        )[0]
        if org_module.bias is not None:
            self.bias = org_module.bias.data.type(self.out_dtype)
        else:
            self.bias = None

    def forward(self, inp):
        assert inp.shape[-1] == self.in_features, "GEMM not possible"
        inputmat = inp.view((-1, self.in_features))
        inputmat = torch.ops.hpu.cast_to_fp8_v2(inputmat, self.scale, False, False)[0]
        out = torch.ops.hpu.fp8_gemm_v2(
            inputmat,
            False,
            self.weight,
            True,
            None,
            self.out_dtype,
            self.scale_inv, # inv is used for recover scale
            self.weight_scale_inv,
            self.bias,
            False,
        )
        return out.view(-1, *inp.shape[1:-1], out.shape[-1])

    def extra_repr(self) -> str:
        import os
        return 'in_features={}, out_features={}, bias={}, scale={}, format={}'.format(
            self.in_features, self.out_features, self.bias is not None, self.scale, self.dtype,
        )

# pragma: no cover
class FP8Matmul(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.out_dtype = torch.float32
        self.register_buffer("input1_scale", torch.tensor(1.0).to('hpu'))
        self.register_buffer("input2_scale", torch.tensor(1.0).to('hpu'))

    def forward(self, input1, input2, transpose1=False, transpose2=False):
        dim1 = input1.shape[-1] if not transpose1 else input1.shape[-2]
        dim2 = input2.shape[-2] if not transpose2 else input2.shape[-1]
        assert dim1 == dim2, "GEMM not possible"

        input1_scale_inv = 1.0 / self.input1_scale
        input2_scale_inv = 1.0 / self.input2_scale
        input1 = torch.ops.hpu.cast_to_fp8_v2(input1, self.input1_scale, False, False)[0]
        input2 = torch.ops.hpu.cast_to_fp8_v2(input2, self.input2_scale, False, False)[0]
        out = torch.ops.hpu.fp8_gemm_v2(
            input1,
            transpose1,
            input2,
            transpose2,
            None,
            self.out_dtype,
            input1_scale_inv, # inv is used for recover scale
            input2_scale_inv,
            None,
            False,
        )
        return out

    def extra_repr(self) -> str:
        import os
        return 'format={}'.format(
            'E4M3' if os.getenv('PT_USE_FP8_143') is not None else 'E5M2',
        )

class FP8BatchMatmul(torch.nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.out_dtype = torch.float32
        self.register_buffer("input1_scale", torch.tensor(1.0).to('hpu'))
        self.register_buffer("input2_scale", torch.tensor(1.0).to('hpu'))

    def forward(self, input1, input2, transpose1=False, transpose2=False):
        dim1 = input1.shape[-1] if not transpose1 else input1.shape[-2]
        dim2 = input2.shape[-2] if not transpose2 else input2.shape[-1]
        assert dim1 == dim2, "GEMM not possible"

        input1_scale_inv = 1.0 / self.input1_scale
        input2_scale_inv = 1.0 / self.input2_scale
        input1 = torch.ops.hpu.cast_to_fp8_v2(input1, self.input1_scale, False, False)[0]
        input2 = torch.ops.hpu.cast_to_fp8_v2(input2, self.input2_scale, False, False)[0]
        out = torch.ops.hpu.fp8_gemm_v2(
            input1,
            transpose1,
            input2,
            transpose2,
            None,
            self.out_dtype,
            input1_scale_inv, # inv is used for recover scale
            input2_scale_inv,
            None,
            False,
        )
        return out

    def extra_repr(self) -> str:
        import os
        return 'format={}'.format(
            'E4M3' if os.getenv('PT_USE_FP8_143') is not None else 'E5M2',
        )

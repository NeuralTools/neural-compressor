# -*- coding: utf-8 -*-
# Copyright (c) 2023 Intel Corporation
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
"""The Tensorflow diagnosis class."""
from typing import Optional

from neural_insights.components.diagnosis.diagnosis import Diagnosis
from neural_insights.components.model.repository import ModelRepository
from neural_insights.components.model.tensorflow.model import TensorflowModel
from neural_insights.components.workload_manager.workload import Workload


class TensorflowDiagnosis(Diagnosis):
    """OnnxRuntime diagnosis class."""

    def __init__(self, workload: Workload):
        """Initialize TensorflowDiagnosis."""
        super().__init__(workload)
        self._model: Optional[TensorflowModel] = None

    @property
    def model(self) -> TensorflowModel:
        """Get Neural Compressor Model instance."""
        self._ensure_model()
        return self._model  # type: ignore

    def _ensure_model(self) -> None:
        """Create Neural Insights Model instance if needed."""
        if self._model is not None:
            return
        model_repository = ModelRepository()
        self._model = model_repository.get_model(self.model_path)  # type: ignore

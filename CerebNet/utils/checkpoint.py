# Copyright 2022 Image Analysis Lab, German Center for Neurodegenerative Diseases (DZNE), Bonn
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# IMPORTS
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    import yacs

from FastSurferCNN.utils import logging
from FastSurferCNN.utils.checkpoint import (
    FASTSURFER_ROOT,
    load_from_checkpoint,
    create_checkpoint_dir,
    get_checkpoint,
    get_checkpoint_path,
    save_checkpoint,
)

logger = logging.get_logger(__name__)

# Defaults
URL = "https://b2share.fz-juelich.de/api/files/c6cf7bc6-2ae5-4d0e-814d-2a3cf0e1a8c5"
CEREBNET_AXI = FASTSURFER_ROOT / "checkpoints/CerebNet_axial_v1.0.0.pkl"
CEREBNET_COR = FASTSURFER_ROOT / "checkpoints/CerebNet_coronal_v1.0.0.pkl"
CEREBNET_SAG = FASTSURFER_ROOT / "checkpoints/CerebNet_sagittal_v1.0.0.pkl"


def is_checkpoint_epoch(cfg: "yacs.config.CfgNode", cur_epoch: int) -> bool:
    """
    Check if checkpoint need to be saved.

    Parameters
    ----------
    cfg : yacs.config.CfgNode
        The config node.
    cur_epoch : int
        The current epoch number to check if this is the last epoch.
    """
    final_epoch = (cur_epoch + 1) == cfg.TRAIN.NUM_EPOCHS
    return (cur_epoch + 1) % cfg.TRAIN.CHECKPOINT_PERIOD or final_epoch

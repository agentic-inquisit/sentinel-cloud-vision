"""Temporal Convolutional Network (dilated causal 1-D convolutions).

Two heads share the same body:
  * mode="segmenter" -> one label per time step  (output: B x K x T)
  * mode="sequence"  -> one label per sequence   (output: B x K, from the last step)

There are no public pretrained weights for these models: train them on your own
data, then write a checkpoint with save_tcn_checkpoint() and upload it to the
TCN weights bucket (see README.md).
"""

from __future__ import annotations

from typing import Sequence

import torch
import torch.nn.functional as F
from torch import nn

MODES = ("segmenter", "sequence")


class _CausalBlock(nn.Module):
    def __init__(self, c_in: int, c_out: int, kernel_size: int, dilation: int, dropout: float):
        super().__init__()
        self.pad = (kernel_size - 1) * dilation
        self.conv1 = nn.Conv1d(c_in, c_out, kernel_size, dilation=dilation)
        self.conv2 = nn.Conv1d(c_out, c_out, kernel_size, dilation=dilation)
        self.drop = nn.Dropout(dropout)
        self.down = nn.Conv1d(c_in, c_out, 1) if c_in != c_out else nn.Identity()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y = self.drop(F.relu(self.conv1(F.pad(x, (self.pad, 0)))))
        y = self.drop(F.relu(self.conv2(F.pad(y, (self.pad, 0)))))
        return F.relu(y + self.down(x))


class TCN(nn.Module):
    def __init__(
        self,
        in_channels: int,
        num_classes: int,
        channels: Sequence[int] = (64, 64, 64, 64),
        kernel_size: int = 3,
        dropout: float = 0.1,
        mode: str = "segmenter",
    ):
        super().__init__()
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self.in_channels = in_channels
        blocks, c = [], in_channels
        for i, h in enumerate(channels):
            blocks.append(_CausalBlock(c, h, kernel_size, 2**i, dropout))
            c = h
        self.body = nn.Sequential(*blocks)
        self.head = nn.Conv1d(c, num_classes, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: (batch, channels, time)."""
        logits = self.head(self.body(x))
        return logits if self.mode == "segmenter" else logits[:, :, -1]


def save_tcn_checkpoint(model: TCN, labels: Sequence[str], path: str, **config) -> None:
    """Save weights plus everything needed to rebuild the model.

    Pass the same keyword arguments you gave TCN(...), e.g.
    save_tcn_checkpoint(model, ["idle", "walk"], "tcn-segmenter.pt",
                    in_channels=3, num_classes=2, mode="segmenter")
    """
    if len(labels) != config.get("num_classes"):
        raise ValueError("len(labels) must equal num_classes")
    config = {k: list(v) if isinstance(v, tuple) else v for k, v in config.items()}
    torch.save({"config": config, "labels": list(labels), "state_dict": model.state_dict()}, path)


def load_tcn_checkpoint(path: str) -> tuple[TCN, list[str]]:
    ckpt = torch.load(path, map_location="cpu", weights_only=True)
    model = TCN(**ckpt["config"])
    model.load_state_dict(ckpt["state_dict"])
    return model.eval(), list(ckpt["labels"])

from torch import nn
from .layers import BottleneckABN, Conv2dABN, DenseABN

def build_conv_layer(input_channel,
                     output_channel,
                     blocks=0,
                     stride=1,
                     skip=True,
                     kernel_size=None,
                     abn=1):

    if kernel_size is None:
        if stride == 2:
            kernel_size = 4
        else:
            kernel_size = 3
    pad = (kernel_size-1) // 2
    if stride ==1 and input_channel == output_channel and skip:
        layers = []
    else:
        layers = [
            Conv2dABN(
                input_channel,
                output_channel,
                kernel_size,
                stride,
                padding=pad,
                abn=abn
            )
        ]

    for i in range(blocks):
        layers.append(BottleneckABN(output_channel, abn=abn))

    return nn.Sequential(*layers)


def build_dense_layer(input_channel,
                     stride=1,
                     kernel_size=None,
                     abn=1,
                     num_layer = 2,
                     growth_rate = 4,):

    if kernel_size is None:
        if stride == 2:
            kernel_size = 4
        else:
            kernel_size = 3
    pad = (kernel_size-1) // 2

    layers = []
    for i in range(num_layer):
        layers.append(
            DenseABN(
                input_channel+i*growth_rate,
                growth_rate,
                stride,
                abn=abn
            ))
    return nn.Sequential(*layers)

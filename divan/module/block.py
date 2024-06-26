import torch
import torch.nn as nn
import torch.nn.functional as F

from divan.module.kan_convs import *
from divan.module.utils import *
from divan.module.conv import *

__all__ = ["Pool_Conv", "C1", "C2", "C2f", "C3", "C2f_KAN", "C3_KAN"]

class Pool_Conv(nn.Module):
    def __init__(self,
                 hidden__channels=16,
                 kernel_size=3,
                 _conv=Conv,
                 activation='ReLU',
                 pool=['Avg', 'Max'],
                 ):
        super(Pool_Conv, self).__init__()

        for p in pool:
            assert p in ['Avg', 'Max']

        in_channels = 1
        self.kernel_size = kernel_size

        self.pool_types = [getattr(nn, 'Adaptive' + p + 'Pool3d')((1, None, None)) for p in pool]

        ff_act = Activations(act_name=activation)

        _Sequential = (
                [_conv(in_channels, hidden__channels, self.kernel_size),
                 ff_act(),
                 _conv(hidden__channels, in_channels, self.kernel_size)])

        self.mlp = nn.Sequential(*_Sequential)
    def forward(self, x):
        y = None
        for pool_type in self.pool_types:
            pool_x = pool_type(x)
            out = self.mlp(pool_x)

            if y is None:
                y = out
            else:
                y += out

        return y



class C1(nn.Module):
    """CSP Bottleneck with 1 convolution."""

    def __init__(self, c1, c2, n=1):
        """Initializes the CSP Bottleneck with configurations for 1 convolution with arguments ch_in, ch_out, number."""
        super().__init__()
        self.cv1 = Conv(c1, c2, 1, 1)
        self.m = nn.Sequential(*(Conv(c2, c2, 3) for _ in range(n)))

    def forward(self, x):
        """Applies cross-convolutions to input in the C3 module."""
        y = self.cv1(x)
        return self.m(y) + y


class C2(nn.Module):
    def __init__(self, c1, c2, n=1, _conv=Conv, shortcut=True, replace_mode=0, g=1, e=0.5):
        """Initializes the CSP Bottleneck with 2 convolutions module with arguments ch_in, ch_out, number, shortcut,
        groups, expansion.
        """
        super().__init__()
        assert 0 <= replace_mode <= 2
        run_conv = _conv if replace_mode >= 2 else Conv

        self.c = int(c2 * e)  # hidden channels
        self.cv1 = run_conv(c1, 2 * self.c, 1, 1)
        self.cv2 = run_conv(2 * self.c, c2, 1)  # optional act=FReLU(c2)
        self.m = nn.Sequential(*(Bottleneck(self.c, self.c, _conv, replace_mode, shortcut, g, k=((3, 3), (3, 3)), e=1.0) for _ in range(n)))

    def forward(self, x):
        """Forward pass through the CSP bottleneck with 2 convolutions."""
        a, b = self.cv1(x).chunk(2, 1)
        return self.cv2(torch.cat((self.m(a), b), 1))

class C2f(nn.Module):
    """Faster Implementation of CSP Bottleneck with 2 convolutions."""
    def __init__(self, c1, c2, n=1, _conv=Conv, shortcut=False, replace_mode=0, g=1, e=0.5):
        """Initialize CSP bottleneck layer with two convolutions with arguments ch_in, ch_out, number, shortcut, groups,
        expansion.
        """
        super().__init__()
        assert 0 <= replace_mode <= 2
        run_conv = _conv if replace_mode >= 2 else Conv

        self.c = int(c2 * e)  # hidden channels
        self.cv1 = run_conv(c1, 2 * self.c, 1, 1)
        self.cv2 = run_conv((2 + n) * self.c, c2, 1)  # optional act=FReLU(c2)
        self.m = nn.ModuleList(Bottleneck(self.c, self.c, _conv, replace_mode, shortcut, g, k=((3, 3), (3, 3)), e=1.0) for _ in range(n))

    def forward(self, x):
        """Forward pass through C2f layer."""
        y = list(self.cv1(x).chunk(2, 1))
        y.extend(m(y[-1]) for m in self.m)
        return self.cv2(torch.cat(y, 1))

class C3(nn.Module):
    def __init__(self, c1, c2, n=1, _conv=Conv, shortcut=True, replace_mode=0, g=1, e=0.5):
        """Initialize the CSP Bottleneck with given channels, number, shortcut, groups, and expansion values."""
        super().__init__()
        assert 0 <= replace_mode <= 2
        run_conv = _conv if replace_mode >= 2 else Conv

        c_ = int(c2 * e)  # hidden channels
        self.cv1 = run_conv(c1, c_, 1, 1)
        self.cv2 = run_conv(c1, c_, 1, 1)
        self.cv3 = run_conv(2 * c_, c2, 1)  # optional act=FReLU(c2)
        self.m = nn.Sequential(*(Bottleneck(c_, c_, _conv, replace_mode, shortcut, g, k=((1, 1), (3, 3)), e=1.0) for _ in range(n)))

    def forward(self, x):
        """Forward pass through the CSP bottleneck with 2 convolutions."""
        return self.cv3(torch.cat((self.m(self.cv1(x)), self.cv2(x)), 1))

class C3x(C3):
    """C3 module with cross-convolutions."""
    def __init__(self, c1, c2, n=1, shortcut=True, g=1, e=0.5):
        """Initialize C3TR instance and set default parameters."""
        super().__init__(c1, c2, n, shortcut, g, e)
        self.c_ = int(c2 * e)
        self.m = nn.Sequential(*(Bottleneck(self.c_, self.c_, shortcut, g, k=((1, 3), (3, 1)), e=1) for _ in range(n)))

class Bottleneck(nn.Module):
    """Standard bottleneck."""
    def __init__(self, c1, c2, _conv, replace_mode=0, shortcut=True, g=1, k=(3, 3), e=0.5):
        """Initializes a bottleneck module with given input/output channels, shortcut option, group, kernels, and
        expansion.
        """
        super().__init__()
        assert 0 <= replace_mode <= 2
        run_conv1 = _conv if replace_mode >= 1 else Conv
        run_conv2 = _conv if replace_mode >= 0 else Conv

        c_ = int(c2 * e)  # hidden channels
        try:
            self.cv1 = run_conv1(c_, c2, k[1], 1, groups=g)
        except:
            self.cv1 = run_conv1(c_, c2, k[1])

        try:
            self.cv2 = run_conv2(c_, c2, k[1], 1, groups=g)
        except:
            self.cv2 = run_conv2(c_, c2, k[1])

        self.add = shortcut and c1 == c2

    def forward(self, x):
        return x + self.cv2(self.cv1(x)) if self.add else self.cv2(self.cv1(x))

class Bottleneck_KAN(nn.Module):
    def __init__(self, c1, c2, _kan, shortcut=True, g=1, k=(3, 3), e=0.5):
        super().__init__()
        c_ = int(c2 * e)  # hidden channels

        self.cv1 = _kan(c1, c_, kernel_size=k[0], padding=k[0] // 2)
        self.cv2 = _kan(c_, c2, kernel_size=k[1], padding=k[1] // 2)

        self.add = shortcut and c1 == c2

    def forward(self, x):
        return x + self.cv2(self.cv1(x)) if self.add else self.cv2(self.cv1(x))

class C2f_KAN(C2f):
    def __init__(self, c1, c2, n=1, _conv=FastKANConv, shortcut=False, g=1, e=0.5):
        super().__init__(c1, c2, n, _conv,shortcut, g, e)
        self.m = nn.ModuleList(Bottleneck_KAN(self.c, self.c, _conv, shortcut, g, k=(3, 3), e=1.0) for _ in range(n))

class C3_KAN(C3):
    def __init__(self, c1, c2, n=1, _conv=FastKANConv, shortcut=False, g=1, e=0.5):
        super().__init__(c1, c2, n, _conv, shortcut, g, e)
        c_ = int(c2 * e)  # hidden channels
        self.m = nn.Sequential(*(Bottleneck_KAN(c_, c_, _conv, shortcut, g, k=(1, 3), e=1.0) for _ in range(n)))

#class Bottleneck_InternImage(nn.Module)

if __name__ == '__main__':

    model = Pool_Conv(16, 3)
    input_rgb = torch.randn(4, 3, 224, 224)

    print(input_rgb.shape)
    print(model(input_rgb).shape)
    input_rgb = torch.randn(4, 2, 224, 224)
    print(input_rgb.shape)
    print(model(input_rgb).shape)
    input_rgb = torch.randn(4, 1, 224, 224)
    print(input_rgb.shape)
    print(model(input_rgb).shape)
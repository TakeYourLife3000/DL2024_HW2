# The code is based on the Yeonwoo Sung's implementation:
# https://github.com/YeonwooSung/Pytorch_mixture-of-experts/blob/main/moe.py
import numpy as np
import torch
import torch.nn as nn
from torch.distributions.normal import Normal

from .fast_kan_conv import FastKANConv
from .kacn_conv import KACNConv
from .kagn_conv import KAGNConv
from .kaln_conv import KALNConv
from .kan_conv import KANConv
from .wav_kan import WavKANConv

__all__ = ["MoEKALNConv", "MoEKANConv", "MoEKAGNConv", "MoEFastKANConv", "MoEKACNConv", "MoEWavKANConv"]

class SparseDispatcher(object):
    """Helper for implementing a mixture of experts.
    The purpose of this class is to create input minibatches for the
    experts and to combine the results of the experts to form a unified
    output tensor.
    There are two functions:
    dispatch - take an input Tensor and create input Tensors for each expert.
    combine - take output Tensors from each expert and form a combined output
      Tensor.  Outputs from different experts for the same batch element are
      summed together, weighted by the provided "gates".
    The class is initialized with a "gates" Tensor, which specifies which
    batch elements go to which experts, and the weights to use when combining
    the outputs.  Batch element b is sent to expert e iff gates[b, e] != 0.
    The inputs and outputs are all two-dimensional [batch, depth].
    Caller is responsible for collapsing additional dimensions prior to
    calling this class and reshaping the output to the original shape.
    See common_layers.reshape_like().
    Example use:
    gates: a float32 `Tensor` with shape `[batch_size, num_experts]`
    inputs: a float32 `Tensor` with shape `[batch_size, input_size]`
    experts: a list of length `num_experts` containing sub-networks.
    dispatcher = SparseDispatcher(num_experts, gates)
    expert_inputs = dispatcher.dispatch(inputs)
    expert_outputs = [experts[i](expert_inputs[i]) for i in range(num_experts)]
    outputs = dispatcher.combine(expert_outputs)
    The preceding code sets the output for a particular example b to:
    output[b] = Sum_i(gates[b, i] * experts[i](inputs[b]))
    This class takes advantage of sparsity in the gate matrix by including in the
    `Tensor`s for expert i only the batch elements for which `gates[b, i] > 0`.
    """

    def __init__(self, num_experts, gates):
        """Create a SparseDispatcher."""

        self._gates = gates
        self._num_experts = num_experts
        # sort experts
        sorted_experts, index_sorted_experts = torch.nonzero(gates).sort(0)
        # drop indices
        _, self._expert_index = sorted_experts.split(1, dim=1)
        # get according batch index for each expert
        self._batch_index = sorted_experts[index_sorted_experts[:, 1], 0]
        # calculate num samples that each expert gets
        self._part_sizes = list((gates > 0).sum(0).cpu().numpy())
        # expand gates to match with self._batch_index
        gates_exp = gates[self._batch_index.flatten()]
        self._nonzero_gates = torch.gather(gates_exp, 1, self._expert_index)

    def dispatch(self, inp):
        """Create one input Tensor for each expert.
        The `Tensor` for a expert `i` contains the slices of `inp` corresponding
        to the batch elements `b` where `gates[b, i] > 0`.
        Args:
          inp: a `Tensor` of shape "[batch_size, <extra_input_dims>]`
        Returns:
          a list of `num_experts` `Tensor`s with shapes
            `[expert_batch_size_i, <extra_input_dims>]`.
        """

        # assigns samples to experts whose gate is nonzero

        # expand according to batch index so we can just split by _part_sizes
        inp_exp = inp[self._batch_index].squeeze(1)
        return torch.split(inp_exp, self._part_sizes, dim=0)

    def combine(self, expert_out, conv_dims, multiply_by_gates=True):
        """Sum together the expert output, weighted by the gates.
        The slice corresponding to a particular batch element `b` is computed
        as the sum over all experts `i` of the expert output, weighted by the
        corresponding gate values.  If `multiply_by_gates` is set to False, the
        gate values are ignored.
        Args:
          expert_out: a list of `num_experts` `Tensor`s, each with shape
            `[expert_batch_size_i, <extra_output_dims>]`.
          multiply_by_gates: a boolean
        Returns:
          a `Tensor` with shape `[batch_size, <extra_output_dims>]`.
        """
        # apply exp to expert outputs, so we are not longer in log space
        stitched = torch.cat(expert_out, 0).exp()
        conv_dims = tuple(1 for _ in range(conv_dims))
        if multiply_by_gates:
            self._nonzero_gates = self._nonzero_gates.view(self._nonzero_gates.shape + conv_dims)
            stitched = stitched.mul(self._nonzero_gates)

        out_size = (self._gates.size(0),) + expert_out[-1].shape[1:]
        zeros = torch.zeros(out_size, requires_grad=True, device=expert_out[-1].device)
        # combine samples that have been processed by the same k experts
        combined = zeros.index_add(0, self._batch_index, stitched.float())
        # add eps to all zero values in order to avoid nans when going back to log space
        combined[combined == 0] = np.finfo(float).eps
        # back to log space
        return combined.log()

    def expert_to_gates(self):
        """Gate values corresponding to the examples in the per-expert `Tensor`s.
        Returns:
          a list of `num_experts` one-dimensional `Tensor`s with type `tf.float32`
              and shapes `[expert_batch_size_i]`
        """
        # split nonzero gates for each expert
        return torch.split(self._nonzero_gates, self._part_sizes, dim=0)


class MoEKANConvBase(nn.Module):
    """Call a Sparsely gated mixture of experts layer with 1-layer Feed-Forward networks as experts.
    Args:
    input_size: integer - size of the input
    output_size: integer - size of the input
    num_experts: an integer - number of experts
    hidden_size: an integer - hidden size of the experts
    noisy_gating: a boolean
    k: an integer - how many experts to use for each batch element
    """

    def __init__(self, conv_class, input_size, output_size, num_experts=16, noisy_gating=True, k=4,
                 kernel_size=3, stride=1, padding=1, **kan_kwargs):
        super(MoEKANConvBase, self).__init__()
        self.noisy_gating = noisy_gating
        self.num_experts = num_experts
        self.output_size = output_size
        self.input_size = input_size
        self.k = k
        # instantiate experts
        self.experts = nn.ModuleList([conv_class(input_size, self.output_size, kernel_size=kernel_size,
                                                 stride=stride, padding=padding, **kan_kwargs) for _ in
                                      range(num_experts)])
        self.w_gate = nn.Parameter(torch.zeros(input_size, num_experts), requires_grad=True)
        self.w_noise = nn.Parameter(torch.zeros(input_size, num_experts), requires_grad=True)

        self.softplus = nn.Softplus()
        self.softmax = nn.Softmax(1)

        if conv_class in [KANConv, FastKANConv, KALNConv, KACNConv, KAGNConv,
                            WavKANConv]:
            self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
            self.conv_dims = 2

        for i in range(1, num_experts):
            self.experts[i].load_state_dict(self.experts[0].state_dict())
        assert (self.k <= self.num_experts)

    def cv_squared(self, x):
        """The squared coefficient of variation of a sample.
        Useful as a loss to encourage a positive distribution to be more uniform.
        Epsilons added for numerical stability.
        Returns 0 for an empty Tensor.
        Args:
        x: a `Tensor`.
        Returns:
        a `Scalar`.
        """
        eps = 1e-10
        # if only num_experts = 1
        if x.shape[0] == 1:
            return torch.Tensor([0])
        return x.float().var() / (x.float().mean() ** 2 + eps)

    def _gates_to_load(self, gates):
        """Compute the true load per expert, given the gates.
        The load is the number of examples for which the corresponding gate is >0.
        Args:
        gates: a `Tensor` of shape [batch_size, n]
        Returns:
        a float32 `Tensor` of shape [n]
        """
        return (gates > 0).sum(0)

    def _prob_in_top_k(self, clean_values, noisy_values, noise_stddev, noisy_top_values):
        """Helper function to NoisyTopKGating.
        Computes the probability that value is in top k, given different random noise.
        This gives us a way of backpropagating from a loss that balances the number
        of times each expert is in the top k experts per example.
        In the case of no noise, pass in None for noise_stddev, and the result will
        not be differentiable.
        Args:
        clean_values: a `Tensor` of shape [batch, n].
        noisy_values: a `Tensor` of shape [batch, n].  Equal to clean values plus
          normally distributed noise with standard deviation noise_stddev.
        noise_stddev: a `Tensor` of shape [batch, n], or None
        noisy_top_values: a `Tensor` of shape [batch, m].
           "values" Output of tf.top_k(noisy_top_values, m).  m >= k+1
        Returns:
        a `Tensor` of shape [batch, n].
        """

        batch = clean_values.size(0)
        m = noisy_top_values.size(1)
        top_values_flat = noisy_top_values.flatten()
        threshold_positions_if_in = torch.arange(batch, device=clean_values.device) * m + self.k
        threshold_if_in = torch.unsqueeze(torch.gather(top_values_flat, 0, threshold_positions_if_in), 1)
        is_in = torch.gt(noisy_values, threshold_if_in)
        threshold_positions_if_out = threshold_positions_if_in - 1
        threshold_if_out = torch.unsqueeze(torch.gather(top_values_flat, 0, threshold_positions_if_out), 1)
        # is each value currently in the top k.
        normal = Normal(torch.tensor([0.0], device=clean_values.device),
                        torch.tensor([1.0], device=clean_values.device))
        prob_if_in = normal.cdf((clean_values - threshold_if_in) / noise_stddev)
        prob_if_out = normal.cdf((clean_values - threshold_if_out) / noise_stddev)
        prob = torch.where(is_in, prob_if_in, prob_if_out)
        return prob

    def noisy_top_k_gating(self, x, train, noise_epsilon=1e-2):
        """Noisy top-k gating.
          See paper: https://arxiv.org/abs/1701.06538.
          Args:
            x: input Tensor with shape [batch_size, input_size]
            train: a boolean - we only add noise at training time.
            noise_epsilon: a float
          Returns:
            gates: a Tensor with shape [batch_size, num_experts]
            load: a Tensor with shape [num_experts]
        """
        clean_logits = x @ self.w_gate
        if self.noisy_gating:
            raw_noise_stddev = x @ self.w_noise
            noise_stddev = ((self.softplus(raw_noise_stddev) + noise_epsilon) * train)
            noisy_logits = clean_logits + (torch.randn_like(clean_logits) * noise_stddev)
            logits = noisy_logits
        else:
            logits = clean_logits

        # calculate topk + 1 that will be needed for the noisy gates
        top_logits, top_indices = logits.topk(min(self.k + 1, self.num_experts), dim=1)
        top_k_logits = top_logits[:, :self.k]
        top_k_indices = top_indices[:, :self.k]
        top_k_gates = self.softmax(top_k_logits)

        zeros = torch.zeros_like(logits, requires_grad=True, device=x.device)
        gates = zeros.scatter(1, top_k_indices, top_k_gates)

        if self.noisy_gating and self.k < self.num_experts and train:
            load = (self._prob_in_top_k(clean_logits, noisy_logits, noise_stddev, top_logits)).sum(0)
        else:
            load = self._gates_to_load(gates)
        return gates, load

    def forward(self, x, train=True, loss_coef=1):
        """Args:
        x: tensor shape [batch_size, input_size]
        train: a boolean scalar.
        loss_coef: a scalar - multiplier on load-balancing losses
        Returns:
        y: a tensor with shape [batch_size, output_size].
        extra_training_loss: a scalar.  This should be added into the overall
        training loss of the model.  The backpropagation of this loss
        encourages all experts to be approximately equally used across a batch.
        """

        gate_x = torch.flatten(self.avgpool(x), 1)

        gates, load = self.noisy_top_k_gating(gate_x, train)
        # calculate importance loss
        importance = gates.sum(0)
        #
        loss = self.cv_squared(importance) + self.cv_squared(load)
        loss *= loss_coef

        dispatcher = SparseDispatcher(self.num_experts, gates)
        expert_inputs = dispatcher.dispatch(x)
        gates = dispatcher.expert_to_gates()
        expert_outputs = [self.experts[i](expert_inputs[i]) for i in range(self.num_experts)]
        y = dispatcher.combine(expert_outputs, self.conv_dims)
        return y, loss

class MoEKALNConv(MoEKANConvBase):
    def __init__(self, input_size, output_size, num_experts=16, noisy_gating=True, k=4,
                 kernel_size=3, stride=1, padding=1, **kan_kwargs):
        super(MoEKALNConv, self).__init__(KALNConv, input_size, output_size, num_experts=num_experts,
                                                 noisy_gating=noisy_gating, k=k,
                                                 kernel_size=kernel_size, stride=stride, padding=padding, **kan_kwargs)

class MoEKANConv(MoEKANConvBase):
    def __init__(self, input_size, output_size, num_experts=16, noisy_gating=True, k=4,
                 kernel_size=3, stride=1, padding=1, **kan_kwargs):
        super(MoEKANConv, self).__init__(KANConv, input_size, output_size, num_experts=num_experts,
                                                noisy_gating=noisy_gating, k=k,
                                                kernel_size=kernel_size, stride=stride, padding=padding, **kan_kwargs)

class MoEKAGNConv(MoEKANConvBase):
    def __init__(self, input_size, output_size, num_experts=16, noisy_gating=True, k=4,
                 kernel_size=3, stride=1, padding=1, **kan_kwargs):
        super(MoEKAGNConv, self).__init__(KAGNConv, input_size, output_size, num_experts=num_experts,
                                                 noisy_gating=noisy_gating, k=k,
                                                 kernel_size=kernel_size, stride=stride, padding=padding, **kan_kwargs)

class MoEFastKANConv(MoEKANConvBase):
    def __init__(self, input_size, output_size, num_experts=16, noisy_gating=True, k=4,
                 kernel_size=3, stride=1, padding=1, **kan_kwargs):
        super(MoEFastKANConv, self).__init__(FastKANConv, input_size, output_size,
                                                    num_experts=num_experts,
                                                    noisy_gating=noisy_gating, k=k,
                                                    kernel_size=kernel_size, stride=stride, padding=padding,
                                                    **kan_kwargs)

class MoEKACNConv(MoEKANConvBase):
    def __init__(self, input_size, output_size, num_experts=16, noisy_gating=True, k=4,
                 kernel_size=3, stride=1, padding=1, **kan_kwargs):
        super(MoEKACNConv, self).__init__(KACNConv, input_size, output_size, num_experts=num_experts,
                                                 noisy_gating=noisy_gating, k=k,
                                                 kernel_size=kernel_size, stride=stride, padding=padding, **kan_kwargs)

class MoEWavKANConv(MoEKANConvBase):
    def __init__(self, input_size, output_size, num_experts=16, noisy_gating=True, k=4,
                 kernel_size=3, stride=1, padding=1, **kan_kwargs):
        super(MoEWavKANConv, self).__init__(WavKANConv, input_size, output_size, num_experts=num_experts,
                                                   noisy_gating=noisy_gating, k=k,
                                                   kernel_size=kernel_size, stride=stride, padding=padding,
                                                   **kan_kwargs)


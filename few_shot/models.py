from torch import nn
import numpy as np
import torch.nn.functional as F
from torch.autograd import Function
import torch
from typing import Dict


from distributions import Bernoulli as BernoulliREINFORCE
from distributions import Round as RoundREINFORCE

##########
# Layers #
##########
class Flatten(nn.Module):
    """Converts N-dimensional Tensor of shape [batch_size, d1, d2, ..., dn] to 2-dimensional Tensor
    of shape [batch_size, d1*d2*...*dn].

    # Arguments
        input: Input tensor
    """
    def forward(self, input):
        return input.view(input.size(0), -1)


class GlobalMaxPool1d(nn.Module):
    """Performs global max pooling over the entire length of a batched 1D tensor

    # Arguments
        input: Input tensor
    """
    def forward(self, input):
        return nn.functional.max_pool1d(input, kernel_size=input.size()[2:]).view(-1, input.size(1))


class GlobalAvgPool2d(nn.Module):
    """Performs global average pooling over the entire height and width of a batched 2D tensor

    # Arguments
        input: Input tensor
    """
    def forward(self, input):
        return nn.functional.avg_pool2d(input, kernel_size=input.size()[2:]).view(-1, input.size(1))


def conv_block(in_channels: int, out_channels: int, conv_size=3) -> nn.Module:
    """Returns a Module that performs 3x3 convolution, ReLu activation, 2x2 max pooling.

    # Arguments
        in_channels:
        out_channels:
    """
    return nn.Sequential(
        nn.Conv2d(in_channels, out_channels, conv_size, padding=1),
        nn.BatchNorm2d(out_channels),
        nn.ReLU(),
        nn.MaxPool2d(kernel_size=2, stride=2)
    )


def deconv_block(in_channels: int, out_channels: int, conv_size=3) -> nn.Module:
    """Returns a Module that performs 3x3 convolution, ReLu activation, 2x2 max pooling.

    # Arguments
        in_channels:
        out_channels:
    """
    return nn.Sequential(
        nn.ConvTranspose2d(in_channels, out_channels, conv_size, padding=1),
        nn.BatchNorm2d(out_channels),
        nn.ReLU()
    )


def functional_conv_block(x: torch.Tensor, weights: torch.Tensor, biases: torch.Tensor,
                          bn_weights, bn_biases) -> torch.Tensor:
    """Performs 3x3 convolution, ReLu activation, 2x2 max pooling in a functional fashion.

    # Arguments:
        x: Input Tensor for the conv block
        weights: Weights for the convolutional block
        biases: Biases for the convolutional block
        bn_weights:
        bn_biases:
    """
    x = F.conv2d(x, weights, biases, padding=1)
    x = F.batch_norm(x, running_mean=None, running_var=None, weight=bn_weights, bias=bn_biases, training=True)
    x = F.relu(x)
    x = F.max_pool2d(x, kernel_size=2, stride=2)
    return x


##########
# Models #
##########
def get_few_shot_encoder(num_input_channels=1) -> nn.Module:
    """Creates a few shot encoder as used in Matching and Prototypical Networks

    # Arguments:
        num_input_channels: Number of color channels the model expects input data to contain. Omniglot = 1,
            miniImageNet = 3
    """
    return nn.Sequential(
        conv_block(num_input_channels, 64),
        conv_block(64, 64),
        conv_block(64, 64),
        conv_block(64, 64),
        Flatten(),
    )


class FewShotClassifier(nn.Module):
    def __init__(self, num_input_channels: int, k_way: int, final_layer_size: int = 64):
        """Creates a few shot classifier as used in MAML.

        This network should be identical to the one created by `get_few_shot_encoder` but with a
        classification layer on top.

        # Arguments:
            num_input_channels: Number of color channels the model expects input data to contain. Omniglot = 1,
                miniImageNet = 3
            k_way: Number of classes the model will discriminate between
            final_layer_size: 64 for Omniglot, 1600 for miniImageNet
        """
        super(FewShotClassifier, self).__init__()
        self.conv1 = conv_block(num_input_channels, 64)
        self.conv2 = conv_block(64, 64)
        self.conv3 = conv_block(64, 64)
        self.conv4 = conv_block(64, 64)

        self.logits = nn.Linear(final_layer_size, k_way)

    def forward(self, x):
        x = self.conv1(x)
        # print("conv1 shape:", x.shape)
        x = self.conv2(x)
        x = self.conv3(x)
        x = self.conv4(x)

        x = x.view(x.size(0), -1)

        return self.logits(x)

    def functional_forward(self, x, weights):
        """Applies the same forward pass using PyTorch functional operators using a specified set of weights."""

        for block in [1, 2, 3, 4]:
            x = functional_conv_block(x, weights['conv'+str(block)+'.0.weight'], weights['conv'+str(block)+'.0.bias'],
                                      weights.get('conv'+str(block)+'.1.weight'), weights.get('conv'+str(block)+'.1.bias'))

        x = x.view(x.size(0), -1)

        x = F.linear(x, weights['logits.weight'], weights['logits.bias'])

        return x

    def view(self, x):
        x1 = self.conv1(x)
        x2 = self.conv2(x1)
        x3 = self.conv3(x2)
        x4 = self.conv4(x3)

        return x1, x2, x3, x4

class MatchingNetwork(nn.Module):
    def __init__(self, n: int, k: int, q: int, fce: bool, num_input_channels: int,
                 lstm_layers: int, lstm_input_size: int, unrolling_steps: int, device: torch.device):
        """Creates a Matching Network as described in Vinyals et al.

        # Arguments:
            n: Number of examples per class in the support set
            k: Number of classes in the few shot classification task
            q: Number of examples per class in the query set
            fce: Whether or not to us fully conditional embeddings
            num_input_channels: Number of color channels the model expects input data to contain. Omniglot = 1,
                miniImageNet = 3
            lstm_layers: Number of LSTM layers in the bidrectional LSTM g that embeds the support set (fce = True)
            lstm_input_size: Input size for the bidirectional and Attention LSTM. This is determined by the embedding
                dimension of the few shot encoder which is in turn determined by the size of the input data. Hence we
                have Omniglot -> 64, miniImageNet -> 1600.
            unrolling_steps: Number of unrolling steps to run the Attention LSTM
            device: Device on which to run computation
        """
        super(MatchingNetwork, self).__init__()
        self.n = n
        self.k = k
        self.q = q
        self.fce = fce
        self.num_input_channels = num_input_channels
        self.encoder = get_few_shot_encoder(self.num_input_channels)
        if self.fce:
            self.g = BidrectionalLSTM(lstm_input_size, lstm_layers).to(device, dtype=torch.double)
            self.f = AttentionLSTM(lstm_input_size, unrolling_steps=unrolling_steps).to(device, dtype=torch.double)

    def forward(self, inputs):
        pass


class BidrectionalLSTM(nn.Module):
    def __init__(self, size: int, layers: int):
        """Bidirectional LSTM used to generate fully conditional embeddings (FCE) of the support set as described
        in the Matching Networks paper.

        # Arguments
            size: Size of input and hidden layers. These are constrained to be the same in order to implement the skip
                connection described in Appendix A.2
            layers: Number of LSTM layers
        """
        super(BidrectionalLSTM, self).__init__()
        self.num_layers = layers
        self.batch_size = 1
        # Force input size and hidden size to be the same in order to implement
        # the skip connection as described in Appendix A.1 and A.2 of Matching Networks
        self.lstm = nn.LSTM(input_size=size,
                            num_layers=layers,
                            hidden_size=size,
                            bidirectional=True)

    def forward(self, inputs):
        # Give None as initial state and Pytorch LSTM creates initial hidden states
        output, (hn, cn) = self.lstm(inputs, None)

        forward_output = output[:, :, :self.lstm.hidden_size]
        backward_output = output[:, :, self.lstm.hidden_size:]

        # g(x_i, S) = h_forward_i + h_backward_i + g'(x_i) as written in Appendix A.2
        # AKA A skip connection between inputs and outputs is used
        output = forward_output + backward_output + inputs
        return output, hn, cn


class AttentionLSTM(nn.Module):
    def __init__(self, size: int, unrolling_steps: int):
        """Attentional LSTM used to generate fully conditional embeddings (FCE) of the query set as described
        in the Matching Networks paper.

        # Arguments
            size: Size of input and hidden layers. These are constrained to be the same in order to implement the skip
                connection described in Appendix A.2
            unrolling_steps: Number of steps of attention over the support set to compute. Analogous to number of
                layers in a regular LSTM
        """
        super(AttentionLSTM, self).__init__()
        self.unrolling_steps = unrolling_steps
        self.lstm_cell = nn.LSTMCell(input_size=size,
                                     hidden_size=size)

    def forward(self, support, queries):
        # Get embedding dimension, d
        if support.shape[-1] != queries.shape[-1]:
            raise(ValueError("Support and query set have different embedding dimension!"))

        batch_size = queries.shape[0]
        embedding_dim = queries.shape[1]

        h_hat = torch.zeros_like(queries).cuda().double()
        c = torch.zeros(batch_size, embedding_dim).cuda().double()

        for k in range(self.unrolling_steps):
            # Calculate hidden state cf. equation (4) of appendix A.2
            h = h_hat + queries

            # Calculate softmax attentions between hidden states and support set embeddings
            # cf. equation (6) of appendix A.2
            attentions = torch.mm(h, support.t())
            attentions = attentions.softmax(dim=1)

            # Calculate readouts from support set embeddings cf. equation (5)
            readout = torch.mm(attentions, support)

            # Run LSTM cell cf. equation (3)
            # h_hat, c = self.lstm_cell(queries, (torch.cat([h, readout], dim=1), c))
            h_hat, c = self.lstm_cell(queries, (h + readout, c))

        h = h_hat + queries

        return h


class Hardsigmoid(nn.Module):

    def __init__(self):
        super(Hardsigmoid, self).__init__()
        self.act = nn.Hardtanh()

    def forward(self, x):
        return (self.act(x) + 1.0) / 2.0


class RoundFunctionST(Function):
    """Rounds a tensor whose values are in [0, 1] to a tensor with values in {0, 1}"""

    @staticmethod
    def forward(ctx, input):
        """Forward pass

        Parameters
        ==========
        :param input: input tensor

        Returns
        =======
        :return: a tensor which is round(input)"""

        # We can cache arbitrary Tensors for use in the backward pass using the
        # save_for_backward method.
        # ctx.save_for_backward(input)

        return torch.round(input)

    @staticmethod
    def backward(ctx, grad_output):
        """In the backward pass we receive a tensor containing the gradient of the
        loss with respect to the output, and we need to compute the gradient of the
        loss with respect to the input.

        Parameters
        ==========
        :param grad_output: tensor that stores the gradients of the loss wrt. output

        Returns
        =======
        :return: tensor that stores the gradients of the loss wrt. input"""

        # This is a pattern that is very convenient - at the top of backward
        # unpack saved_tensors and initialize all gradients w.r.t. inputs to
        # None. Thanks to the fact that additional trailing Nones are
        # ignored, the return statement is simple even when the function has
        # optional inputs.
        # input, weight, bias = ctx.saved_variables

        return grad_output


class BernoulliFunctionST(Function):

    @staticmethod
    def forward(ctx, input):
        return torch.bernoulli(input)

    @staticmethod
    def backward(ctx, grad_output):
        return grad_output


RoundST = RoundFunctionST.apply
BernoulliST = BernoulliFunctionST.apply


class DeterministicBinaryActivation(nn.Module):

    def __init__(self, estimator='ST'):
        super(DeterministicBinaryActivation, self).__init__()

        assert estimator in ['ST', 'REINFORCE']

        self.estimator = estimator
        self.act = Hardsigmoid()

        if self.estimator == 'ST':
            self.binarizer = RoundST
        elif self.estimator == 'REINFORCE':
            self.binarizer = RoundREINFORCE

    def forward(self, input):
        x, slope = input
        x = self.act(slope * x)
        x = self.binarizer(x)
        if self.estimator == 'REINFORCE':
            x = x.sample()
        return x


class StochasticBinaryActivation(nn.Module):

    def __init__(self, estimator='ST'):
        super(StochasticBinaryActivation, self).__init__()

        assert estimator in ['ST', 'REINFORCE']

        self.estimator = estimator
        self.act = Hardsigmoid()

        if self.estimator == 'ST':
            self.binarizer = BernoulliST
        elif self.estimator == 'REINFORCE':
            self.binarizer = BernoulliREINFORCE

    def forward(self, input):
        x, slope = input
        probs = self.act(slope * x)
        out = self.binarizer(probs)
        if self.estimator == 'REINFORCE':
            out = out.sample()
        return out


class SemanticBinaryClassifier(nn.Module):
    def __init__(self, num_input_channels: int, k_way: int, final_layer_size: int = 64,
                 size_dense_layer_before_binary:int =None, size_binary_layer=10, stochastic: bool=True,
                 n_conv_layers: int = 4):
        """
        # Arguments:
            num_input_channels: Number of color channels the model expects input data to contain. Omniglot = 1,
                miniImageNet = 3
            k_way: Number of classes the model will discriminate between
            final_layer_size: 64 for Omniglot, 1600 for miniImageNet
            size_binary_layer: Number of neurons in the last hidden layer (with binary activation)
            size_dense_layer_before_binary: if not None, add a dense layer of this size before the last binary layer
            n_conv_layers: number of convolutional layers, must be on [1-4]
        """
        super(SemanticBinaryClassifier, self).__init__()

        self.conv1 = conv_block(num_input_channels, 64)
        n = 12544
        #TODO adapt calcul n
        if n_conv_layers >= 2:
            self.conv2 = conv_block(64, 64)
            n = 3136
        if n_conv_layers >= 3:
            self.conv3 = conv_block(64, 64)
            n = 576
        if n_conv_layers >= 4:
            self.conv4 = conv_block(64, 64)
            n = 64
        #print("size::", self.conv3.size())
        if size_dense_layer_before_binary is not None:
            self.dense1 = nn.Linear(n, size_dense_layer_before_binary)
            self.dense2 = nn.Linear(size_dense_layer_before_binary, size_binary_layer)
        else:
            self.dense2 = nn.Linear(n, size_binary_layer)

        if stochastic:
            self.binary_act = StochasticBinaryActivation(estimator='ST')
        else:
            self.binary_act = DeterministicBinaryActivation(estimator='ST')
        self.logits = nn.Linear(size_binary_layer, k_way)
        self.slope = 1.0
        self.dense_layer_before_bin = size_dense_layer_before_binary is not None
        self.n_conv_layers = n_conv_layers

    def forward(self, x):
        x = self.conv1(x)
        if self.n_conv_layers >= 2:
            x = self.conv2(x)
        if self.n_conv_layers >= 3:
            x = self.conv3(x)
        if self.n_conv_layers >= 4:
            x = self.conv4(x)
        x = x.view(x.size(0), -1)
        if self.dense_layer_before_bin:
            x = self.dense1(x)
        x = self.dense2(x)
        x = self.binary_act([x, self.slope])

        return self.logits(x), x


class SemanticBinaryAutoEncoder(nn.Module):
    def __init__(self, num_input_channels: int, size_binary_layer: int=10, size_continue_layer: int=10,
                 stochastic: bool=True):
        super(SemanticBinaryAutoEncoder, self).__init__()
        self.size_continue_layer = size_continue_layer
        self.conv1 = conv_block(num_input_channels, 64)
        self.conv2 = conv_block(64, 64)
        self.dense_bin = nn.Linear(3136, size_binary_layer)
        self.dense_cont = nn.Linear(3136, size_continue_layer)

        self.deconv1 = deconv_block(64, 64)
        self.deconv2 = deconv_block(64, 64)
        self.deconv3 = nn.ConvTranspose2d(64, 64, 3)

        if stochastic:
            self.binary_act = StochasticBinaryActivation(estimator='ST')
        else:
            self.binary_act = DeterministicBinaryActivation(estimator='ST')


    def encoder(self, x):
        #  return latent space with binary semantic part and continue example variation
        # batch examples have the same class
        # maybe reduce hamming
        x = self.conv1(x)
        x = self.conv2(x)

        x1 = self.dense_bin(x)
        x2 = self.dense_cont(x)

        x1 = self.binary_act(x1)
        x2 = F.relu(x2)

        return x1, x2

    def decoder(self, latent_space):
        x = self.deconv1(latent_space)
        x = self.deconv2(x)
        x = F.tanh(self.deconv3(x))
        return x

    def binary_space_decoder(self, binary_latent_space, n_noise):
        # construct an example of the same class with a binary_latent_space and a noise vector
        n = torch.distributions.Uniform(0, 1)
        noise = n.sample((n_noise, self.size_continue_layer))
        latent_space = torch.cat((binary_latent_space, noise))
        return self.decoder(latent_space)

    def forward(self, x):
        bina, cont = self.encoder(x)
        x1 = self.decoder(torch.cat((bina, cont)))
        x2 = self.binary_space_decoder(bina)
        return x1, x2

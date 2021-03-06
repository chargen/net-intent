"""Convolutional network example, using AllConvNet and CIFAR-10.

"""
import logging
from argparse import ArgumentParser

import theano
from theano import tensor

from blocks.algorithms import Scale, AdaDelta, GradientDescent, Momentum
from blocks.algorithms import CompositeRule, Restrict
from blocks.algorithms import StepClipping
from blocks.bricks import Rectifier
from blocks.bricks import Activation
from blocks.bricks import Softmax
from blocks.bricks import Linear
from blocks.bricks.conv import Convolutional
from blocks.bricks.cost import CategoricalCrossEntropy, MisclassificationRate
from blocks.extensions import FinishAfter, Timing, Printing, ProgressBar
from blocks.extensions.monitoring import DataStreamMonitoring
from blocks.extensions.monitoring import TrainingDataMonitoring
from blocks.extensions.saveload import Checkpoint, Load
from blocks.filter import VariableFilter
from blocks.graph import apply_dropout
from blocks.graph import ComputationGraph
from blocks.initialization import Constant, Uniform
from blocks.main_loop import MainLoop
from blocks.model import Model
from blocks.monitoring import aggregation
from blocks.roles import BIAS
from blocks.roles import WEIGHT
from blocks.roles import OUTPUT
from blocks_extras.extensions.plot import Plot  
from fuel.datasets import CIFAR10
from fuel.schemes import ShuffledScheme
from fuel.streams import DataStream
from fuel.transformers.image import RandomFixedSizeCrop
from intent.allconv import create_noisy_all_conv_net
from intent.attrib import ComponentwiseCrossEntropy
from intent.attrib import print_attributions
from intent.attrib import save_attributions
from intent.ablation import ConfusionMatrix
from intent.ablation import Sum
from intent.noisy import NITS, NOISE, LOG_SIGMA, NoiseExtension
from intent.noisy import NoisyDataStreamMonitoring
from intent.transform import RandomFlip
from intent.transform import RandomPadCropFlip
from intent.transform import NormalizeBatchLevels
from intent.schedule import EpochSchedule
from intent.schedule import EpochExponentiation
import json
from json import JSONEncoder, dumps
import numpy

# For testing

def main(save_to, num_epochs,
         subset=None, num_batches=None, batch_size=None,
         regularization=None, annealing=None,
         histogram=None, resume=False):
    output_size = 10
    convnet = create_noisy_all_conv_net(batch_size, True)

    x = tensor.tensor4('features')
    y = tensor.lmatrix('targets')

    # Normalize input and apply the convnet
    probs = convnet.apply(x)
    test_cost = (CategoricalCrossEntropy().apply(y.flatten(), probs)
            .copy(name='cost'))
    test_components = (ComponentwiseCrossEntropy().apply(y.flatten(), probs)
            .copy(name='components'))
    test_error_rate = (MisclassificationRate().apply(y.flatten(), probs)
                  .copy(name='error_rate'))
    test_confusion = (ConfusionMatrix().apply(y.flatten(), probs)
                  .copy(name='confusion'))
    test_confusion.tag.aggregation_scheme = Sum(test_confusion)

    test_cg = ComputationGraph([test_cost, test_error_rate, test_components])

    # Apply dropout to all layer outputs except final softmax
    # dropout_vars = VariableFilter(
    #         roles=[OUTPUT], bricks=[Convolutional],
    #         theano_name_regex="^conv_[25]_apply_output$")(test_cg.variables)
    # drop_cg = apply_dropout(test_cg, dropout_vars, 0.5)

    # Apply 0.2 dropout to the pre-averaging layer
    # dropout_vars_2 = VariableFilter(
    #         roles=[OUTPUT], bricks=[Convolutional],
    #         theano_name_regex="^conv_8_apply_output$")(test_cg.variables)
    # train_cg = apply_dropout(test_cg, dropout_vars_2, 0.2)

    # Apply 0.2 dropout to the input, as in the paper
    # train_cg = apply_dropout(test_cg, [x], 0.2)
    # train_cg = drop_cg
    train_cg = test_cg

    train_cost, train_error_rate, train_components = (
            test_cost, test_error_rate, test_components)

    # Apply regularization to the cost

    trainable_parameters = VariableFilter(roles=[WEIGHT, BIAS])(
            train_cg.parameters)
    noise_parameters = VariableFilter(roles=[NOISE])(train_cg.parameters)
    biases = VariableFilter(roles=[BIAS])(train_cg.parameters)
    weights = VariableFilter(roles=[WEIGHT])(train_cg.variables)
    logsigma = VariableFilter(roles=[LOG_SIGMA])(train_cg.variables)

    test_nits = VariableFilter(roles=[NITS])(train_cg.auxiliary_variables)
    test_nit_rate = tensor.concatenate([n.flatten() for n in test_nits]).mean()
    test_nit_rate.name = 'nit_rate'

    train_nit_rate = test_nit_rate

    l2_norm = sum([(W ** 2).sum() for W in weights])
    l2_norm.name = 'l2_norm'
    l2_regularization = 0.0001 * l2_norm
    l2_regularization.name = 'l2_regularization'
    # test_cost = test_cost + l2_regularization
    # test_cost.name = 'cost_with_regularization'

    mean_log_sigma = tensor.concatenate([n.flatten() for n in logsigma]).mean()
    mean_log_sigma.name = 'log_sigma'

    # Training version of cost
    nit_penalty = theano.shared(numpy.asarray(
            regularization, dtype=theano.config.floatX))
    nit_penalty.name = 'nit_penalty'
    train_cost_without_regularization = train_cost
    train_cost_without_regularization.name = 'cost_without_regularization'
    nit_regularization = nit_penalty * train_nit_rate
    nit_regularization.name = 'nit_regularization'
    train_cost = train_cost + nit_regularization + l2_regularization
    train_cost.name = 'cost_with_regularization'

    cifar10_train = CIFAR10(("train",))
    cifar10_train_stream = RandomPadCropFlip(
        NormalizeBatchLevels(DataStream.default_stream(
            cifar10_train, iteration_scheme=ShuffledScheme(
                cifar10_train.num_examples, batch_size)),
        which_sources=('features',)),
        (32, 32), pad=5, which_sources=('features',))
    # cifar10_train_stream = NormalizeBatchLevels(DataStream.default_stream(
    #        cifar10_train, iteration_scheme=ShuffledScheme(
    #             cifar10_train.num_examples, batch_size)),
    #     which_sources=('features',))

    cifar10_test = CIFAR10(("test",))
    cifar10_test_stream = NormalizeBatchLevels(DataStream.default_stream(
        cifar10_test,
        iteration_scheme=ShuffledScheme(
            cifar10_test.num_examples, batch_size)),
        which_sources=('features',))

    momentum = Momentum(0.002, 0.9)

    # Create a step rule that doubles the learning rate of biases, like Caffe.
    # scale_bias = Restrict(Scale(2), biases)
    # step_rule = CompositeRule([scale_bias, momentum])
    step_rule = CompositeRule([StepClipping(10), momentum])

    # Train with simple SGD
    algorithm = GradientDescent(
        cost=train_cost, parameters=trainable_parameters,
        step_rule=step_rule)

    add_noise = NoiseExtension(noise_parameters=noise_parameters)

    # `Timing` extension reports time for reading data, aggregating a batch
    # and monitoring;
    # `ProgressBar` displays a nice progress bar during training.
    extensions = [Timing(),
                  add_noise,
                  FinishAfter(after_n_epochs=num_epochs,
                              after_n_batches=num_batches),
                  EpochSchedule(momentum.learning_rate, [
                      (1, 0.005),
                      (3, 0.01),
                      (5, 0.02),
                      (200, 0.002),
                      (250, 0.0002),
                      (300, 0.00002)
                  ]),
                  NoisyDataStreamMonitoring(
                      [test_cost, test_error_rate, test_nit_rate,
                          test_confusion],
                      cifar10_test_stream,
                      noise_parameters=noise_parameters,
                      prefix="test"),
                  TrainingDataMonitoring(
                      [train_cost, train_error_rate, train_nit_rate,
                       train_cost_without_regularization,
                       l2_norm,
                       nit_penalty,
                       l2_regularization,
                       nit_regularization,
                       mean_log_sigma,
                       momentum.learning_rate,
                       aggregation.mean(algorithm.total_gradient_norm)],
                      prefix="train",
                      every_n_batches=100,
                      after_epoch=True),
                  Plot('Training performance for ' + save_to,
                      channels=[
                          ['train_cost_with_regularization',
                           'train_cost_without_regularization',
                           'train_l2_regularization',
                           'train_nit_regularization'],
                          ['train_error_rate'],
                          ['train_total_gradient_norm'],
                      ],
                      every_n_batches=100),
                  Plot('Test performance for ' + save_to,
                      channels=[[
                          'train_error_rate',
                          'test_error_rate',
                          ]],
                      after_epoch=True),
                  Checkpoint(save_to),
                  ProgressBar(),
                  Printing()]

    if annealing:
        extensions.append(EpochExponentiation(nit_penalty, 1 - annealing))

    if histogram:
        attribution = AttributionExtension(
            components=train_components,
            parameters=trainable_parameters,
            components_size=output_size,
            after_batch=True)
        extensions.insert(0, attribution)

    if resume:
        extensions.append(Load(save_to, True, True))

    model = Model(train_cost)

    main_loop = MainLoop(
        algorithm,
        cifar10_train_stream,
        model=model,
        extensions=extensions)

    main_loop.run()

    if histogram:
        save_attributions(attribution, filename=histogram)

    with open('execution-log.json', 'w') as outfile:
        json.dump(main_loop.log, outfile, cls=NumpyEncoder)

class NumpyEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, numpy.ndarray):
            if obj.ndim == 0:
                return obj + 0
            if obj.ndim == 1:
                return obj.tolist()
            return list([self.default(row) for row in obj])
        return JSONEncoder.default(self, obj)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = ArgumentParser("An example of training a convolutional network "
                            "on the CIFAR dataset.")
    parser.add_argument("--num-epochs", type=int, default=350,
                        help="Number of training epochs to do.")
    parser.add_argument("--batch-size", type=int, default=64,
                        help="Number of training examples per minibatch.")
    parser.add_argument("--histogram", help="histogram file")
    parser.add_argument("save_to", default="cifar10-fixed-rna25.0001w.tar", nargs="?",
                        help="Destination to save the state of the training "
                             "process.")
    parser.add_argument('--regularization', type=float, default=0.01,
                        help="Amount of regularization to apply.")
    parser.add_argument('--annealing', type=float, default=0,
                        help="Rate of annealing to apply.")
    parser.add_argument('--subset', type=int, default=None,
                        help="Size of limited training set.")
    parser.add_argument('--resume', dest='resume', action='store_true')
    parser.add_argument('--no-resume', dest='resume', action='store_false')
    parser.set_defaults(resume=False)
    args = parser.parse_args()
    main(**vars(args))

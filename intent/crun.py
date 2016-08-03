"""Convolutional network example, using AllConvNet and CIFAR-10.

"""
import logging
from argparse import ArgumentParser

from theano import tensor

from blocks.algorithms import Scale, AdaDelta, GradientDescent, Momentum
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
from blocks.roles import WEIGHT
from blocks.roles import OUTPUT
from fuel.datasets import CIFAR10
from fuel.schemes import ShuffledScheme
from fuel.streams import DataStream
from fuel.transformers.image import RandomFixedSizeCrop
from intent.allconv import create_all_conv_net
from intent.attrib import ComponentwiseCrossEntropy
from intent.attrib import print_attributions
from intent.attrib import save_attributions
from intent.ablation import ConfusionMatrix
from intent.ablation import Sum
from intent.flip import RandomFlip
import json
from json import JSONEncoder, dumps
import numpy

# For testing

def main(save_to, num_epochs,
         regularization=0.001, subset=None, num_batches=None,
         histogram=None, resume=False):
    batch_size = 500
    output_size = 10
    convnet = create_all_conv_net()

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
    dropout_vars = VariableFilter(
            roles=[OUTPUT], bricks=[Convolutional, Linear],
            theano_name_regex="^conv_[35]_apply_output$")(test_cg.variables)

    # Apply 0.5 dropout to the post-pooling layers
    drop_cg = apply_dropout(test_cg, dropout_vars, 0.5)

    # Apply 0.2 dropout to the input
    train_cg = apply_dropout(drop_cg, [x], 0.2)

    train_cost, train_error_rate, train_components = train_cg.outputs

    # Apply regularization to the cost
    weights = VariableFilter(roles=[WEIGHT])(train_cg.variables)
    l2_norm = sum([(W ** 2).sum() for W in weights])
    l2_norm.name = 'l2_norm'
    test_cost = test_cost + regularization * l2_norm
    test_cost.name = 'cost_with_regularization'

    # Training version of cost
    train_cost = train_cost + regularization * l2_norm
    train_cost.name = 'cost_with_regularization'

    cifar10_train = CIFAR10(("train",))
    cifar10_train_stream = RandomFlip(RandomFixedSizeCrop(
        DataStream.default_stream(
            cifar10_train, iteration_scheme=ShuffledScheme(
                cifar10_train.num_examples, batch_size)), (27, 27)))

    cifar10_test = CIFAR10(("test",))
    cifar10_test_stream = DataStream.default_stream(
        cifar10_test,
        iteration_scheme=ShuffledScheme(
            cifar10_test.num_examples, batch_size))

    # Train with simple SGD
    algorithm = GradientDescent(
        cost=train_cost, parameters=train_cg.parameters,
        step_rule=AdaDelta())

    # `Timing` extension reports time for reading data, aggregating a batch
    # and monitoring;
    # `ProgressBar` displays a nice progress bar during training.
    extensions = [Timing(),
                  FinishAfter(after_n_epochs=num_epochs,
                              after_n_batches=num_batches),
                  DataStreamMonitoring(
                      [test_cost, test_error_rate, test_confusion],
                      cifar10_test_stream,
                      prefix="test"),
                  TrainingDataMonitoring(
                      [train_cost, train_error_rate, l2_norm,
                       aggregation.mean(algorithm.total_gradient_norm)],
                      prefix="train",
                      after_epoch=True),
                  Checkpoint(save_to),
                  ProgressBar(),
                  Printing()]

    if histogram:
        attribution = AttributionExtension(
            components=train_components,
            parameters=cg.parameters,
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
    parser.add_argument("--num-epochs", type=int, default=5,
                        help="Number of training epochs to do.")
    parser.add_argument("--histogram", help="histogram file")
    parser.add_argument("save_to", default="cifar10.tar", nargs="?",
                        help="Destination to save the state of the training "
                             "process.")
    parser.add_argument('--regularization', type=float, default=0.001,
                        help="Amount of regularization to apply.")
    parser.add_argument('--subset', type=int, default=None,
                        help="Size of limited training set.")
    parser.add_argument('--resume', dest='resume', action='store_true')
    parser.add_argument('--no-resume', dest='resume', action='store_false')
    parser.set_defaults(resume=False)
    args = parser.parse_args()
    main(**vars(args))

# Copyright 2017 reinforce.io. All Rights Reserved.
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
# ==============================================================================

from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import tensorflow as tf

from tensorforce import util
from tensorforce.core.networks import Network
from tensorforce.core.distributions import Distribution, Bernoulli, Categorical, Gaussian, Beta
from tensorforce.models import MemoryModel


class DistributionModel(MemoryModel):
    """
    Base class for models using distributions parametrized by a neural network.
    """

    def __init__(
        self,
        states,
        actions,
        scope,
        device,
        saver,
        summarizer,
        distributed,
        batching_capacity,
        variable_noise,
        states_preprocessing,
        actions_exploration,
        reward_preprocessing,
        update_mode,
        memory,
        optimizer,
        discount,
        network,
        distributions,
        entropy_regularization,
        requires_deterministic
    ):
        self.network_spec = network
        self.distributions_spec = distributions

        # Entropy regularization
        assert entropy_regularization is None or entropy_regularization >= 0.0
        self.entropy_regularization = entropy_regularization

        # For deterministic action sampling (Q vs PG model)
        self.requires_deterministic = requires_deterministic

        self.network = None
        self.distributions = None
        self.fn_kl_divergence = None

        super(DistributionModel, self).__init__(
            states=states,
            actions=actions,
            scope=scope,
            device=device,
            saver=saver,
            summarizer=summarizer,
            distributed=distributed,
            batching_capacity=batching_capacity,
            variable_noise=variable_noise,
            states_preprocessing=states_preprocessing,
            actions_exploration=actions_exploration,
            reward_preprocessing=reward_preprocessing,
            update_mode=update_mode,
            memory=memory,
            optimizer=optimizer,
            discount=discount
        )

    def initialize(self, custom_getter):
        # Network
        self.network = Network.from_spec(
            spec=self.network_spec,
            kwargs=dict(summary_labels=self.summary_labels)
        )

        # Before super-call since internals_spec attribute is required subsequently.
        assert len(self.internals_spec) == 0
        self.internals_spec = self.network.internals_spec()

        super(DistributionModel, self).initialize(custom_getter)

        # Distributions
        self.distributions = self.create_distributions()

        # KL divergence function
        self.fn_kl_divergence = tf.make_template(
            name_='kl-divergence',
            func_=self.tf_kl_divergence,
            custom_getter_=custom_getter
        )

    def create_distributions(self):
        distributions = dict()
        for name, action in self.actions_spec.items():

            if self.distributions_spec is not None and name in self.distributions_spec:
                kwargs = dict(action)
                kwargs['summary_labels'] = self.summary_labels
                distributions[name] = Distribution.from_spec(
                    spec=self.distributions_spec[name],
                    kwargs=kwargs
                )

            elif action['type'] == 'bool':
                distributions[name] = Bernoulli(
                    shape=action['shape'],
                    summary_labels=self.summary_labels
                )

            elif action['type'] == 'int':
                distributions[name] = Categorical(
                    shape=action['shape'],
                    num_actions=action['num_actions'],
                    summary_labels=self.summary_labels
                )

            elif action['type'] == 'float':
                if 'min_value' in action:
                    distributions[name] = Beta(
                        shape=action['shape'],
                        min_value=action['min_value'],
                        max_value=action['max_value'],
                        summary_labels=self.summary_labels
                    )

                else:
                    distributions[name] = Gaussian(
                        shape=action['shape'],
                        summary_labels=self.summary_labels
                    )

        return distributions

    def tf_actions_and_internals(self, states, internals, deterministic):
        embedding, internals = self.network.apply(
            x=states,
            internals=internals,
            update=tf.constant(value=False),
            return_internals=True
        )

        actions = dict()
        for name, distribution in self.distributions.items():
            distr_params = distribution.parameterize(x=embedding)
            actions[name] = distribution.sample(
                distr_params=distr_params,
                deterministic=tf.logical_or(x=deterministic, y=self.requires_deterministic)
            )
            # Prefix named variable with "name_" if more than 1 distribution
            if len(self.distributions.items()) > 1:
                name_prefix = name + "_"
            else:
                name_prefix = ""
            # parameterize() returns list as [logits, probabilities, state_value]
            self.network.set_named_tensor(name_prefix + "logits", distr_params[0])
            self.network.set_named_tensor(name_prefix + "probabilities", distr_params[1])
            self.network.set_named_tensor(name_prefix + "state_value", distr_params[2])

        return actions, internals

    def tf_regularization_losses(self, states, internals, update):
        losses = super(DistributionModel, self).tf_regularization_losses(
            states=states,
            internals=internals,
            update=update
        )

        network_loss = self.network.regularization_loss()
        if network_loss is not None:
            losses['network'] = network_loss

        for distribution in self.distributions.values():
            regularization_loss = distribution.regularization_loss()
            if regularization_loss is not None:
                if 'distributions' in losses:
                    losses['distributions'] += regularization_loss
                else:
                    losses['distributions'] = regularization_loss

        if self.entropy_regularization is not None and self.entropy_regularization > 0.0:
            entropies = list()
            embedding = self.network.apply(x=states, internals=internals, update=update)
            for name, distribution in self.distributions.items():
                distr_params = distribution.parameterize(x=embedding)
                entropy = distribution.entropy(distr_params=distr_params)
                collapsed_size = util.prod(util.shape(entropy)[1:])
                entropy = tf.reshape(tensor=entropy, shape=(-1, collapsed_size))
                entropies.append(entropy)

            entropy_per_instance = tf.reduce_mean(input_tensor=tf.concat(values=entropies, axis=1), axis=1)
            entropy = tf.reduce_mean(input_tensor=entropy_per_instance, axis=0)
            if 'entropy' in self.summary_labels:
                summary = tf.summary.scalar(name='entropy', tensor=entropy)
                self.summaries.append(summary)
            losses['entropy'] = -self.entropy_regularization * entropy

        return losses

    def tf_kl_divergence(self, states, internals, actions, terminal, reward, next_states, next_internals, update, reference=None):
        embedding = self.network.apply(x=states, internals=internals, update=update)
        kl_divergences = list()

        for name, distribution in self.distributions.items():
            distr_params = distribution.parameterize(x=embedding)
            fixed_distr_params = tuple(tf.stop_gradient(input=value) for value in distr_params)
            kl_divergence = distribution.kl_divergence(distr_params1=fixed_distr_params, distr_params2=distr_params)
            collapsed_size = util.prod(util.shape(kl_divergence)[1:])
            kl_divergence = tf.reshape(tensor=kl_divergence, shape=(-1, collapsed_size))
            kl_divergences.append(kl_divergence)

        kl_divergence_per_instance = tf.reduce_mean(input_tensor=tf.concat(values=kl_divergences, axis=1), axis=1)
        return tf.reduce_mean(input_tensor=kl_divergence_per_instance, axis=0)

    def optimizer_arguments(self, states, internals, actions, terminal, reward, next_states, next_internals):
        arguments = super(DistributionModel, self).optimizer_arguments(
            states=states,
            internals=internals,
            actions=actions,
            terminal=terminal,
            reward=reward,
            next_states=next_states,
            next_internals=next_internals
        )
        arguments['fn_kl_divergence'] = self.fn_kl_divergence
        return arguments

    def get_variables(self, include_submodules=False, include_nontrainable=False):
        model_variables = super(DistributionModel, self).get_variables(
            include_submodules=include_submodules,
            include_nontrainable=include_nontrainable
        )

        network_variables = self.network.get_variables(include_nontrainable=include_nontrainable)
        model_variables += network_variables

        distribution_variables = [
            variable for name in sorted(self.distributions)
            for variable in self.distributions[name].get_variables(include_nontrainable=include_nontrainable)
        ]
        model_variables += distribution_variables

        return model_variables

    def get_summaries(self):
        model_summaries = super(DistributionModel, self).get_summaries()
        network_summaries = self.network.get_summaries()
        distribution_summaries = [
            summary for name in sorted(self.distributions)
            for summary in self.distributions[name].get_summaries()
        ]

        return model_summaries + network_summaries + distribution_summaries

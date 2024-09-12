import copy
from pathlib import Path
from unittest.mock import MagicMock

import dill
import numpy as np
import pytest
import torch
import torch.nn as nn
import torch.optim as optim
from accelerate import Accelerator
from accelerate.optimizer import AcceleratedOptimizer

from agilerl.algorithms.matd3 import MATD3
from agilerl.networks.custom_components import GumbelSoftmax
from agilerl.networks.evolvable_cnn import EvolvableCNN
from agilerl.networks.evolvable_mlp import EvolvableMLP
from agilerl.utils.utils import make_multi_agent_vect_envs
from agilerl.wrappers.make_evolvable import MakeEvolvable


class DummyMultiEnv:
    def __init__(self, state_dims, action_dims):
        self.state_dims = state_dims
        self.action_dims = action_dims
        self.agents = ["agent_0", "agent_1"]
        self.possible_agents = ["agent_0", "agent_1"]
        self.metadata = None
        self.observation_space = None
        self.action_space = None

    def reset(self, seed=None, options=None):
        return {agent: np.random.rand(*self.state_dims) for agent in self.agents}, {
            "info_string": None,
            "agent_mask": {"agent_0": False, "agent_1": True},
            "env_defined_actions": {"agent_0": np.array([0, 1]), "agent_1": None},
        }

    def step(self, action):
        return (
            {agent: np.random.rand(*self.state_dims) for agent in self.agents},
            {agent: np.random.randint(0, 5) for agent in self.agents},
            {agent: 1 for agent in self.agents},
            {agent: np.random.randint(0, 2) for agent in self.agents},
            {agent: "info_string" for agent in self.agents},
        )


class MultiAgentCNNActor(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv3d(
            in_channels=4, out_channels=16, kernel_size=(1, 3, 3), stride=4
        )
        self.conv2 = nn.Conv3d(
            in_channels=16, out_channels=32, kernel_size=(1, 3, 3), stride=2
        )
        self.pool = nn.MaxPool2d(kernel_size=2, stride=2)
        self.fc1 = nn.Linear(15200, 256)
        self.fc2 = nn.Linear(256, 2)
        self.relu = nn.ReLU()
        self.output_activation = GumbelSoftmax()

    def forward(self, state_tensor):
        x = self.relu(self.conv1(state_tensor))
        x = self.relu(self.conv2(x))
        x = x.view(x.size(0), -1)
        x = self.relu(self.fc1(x))
        x = self.output_activation(self.fc2(x))

        return x


class MultiAgentCNNCritic(nn.Module):
    def __init__(self):
        super().__init__()
        self.conv1 = nn.Conv3d(
            in_channels=4, out_channels=16, kernel_size=(2, 3, 3), stride=4
        )
        self.conv2 = nn.Conv3d(
            in_channels=16, out_channels=32, kernel_size=(1, 3, 3), stride=2
        )
        self.pool = nn.MaxPool2d(kernel_size=2, stride=2)
        self.fc1 = nn.Linear(15202, 256)
        self.fc2 = nn.Linear(256, 2)
        self.relu = nn.ReLU()

    def forward(self, state_tensor, action_tensor):
        x = self.relu(self.conv1(state_tensor))
        x = self.relu(self.conv2(x))
        x = x.view(x.size(0), -1)
        x = torch.cat([x, action_tensor], dim=1)
        x = self.relu(self.fc1(x))
        x = self.fc2(x)

        return x


class DummyEvolvableMLP(EvolvableMLP):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def forward(self, *args, **kwargs):
        return super().forward(*args, **kwargs)

    def no_sync(self):
        class DummyNoSync:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                pass  # Add cleanup or handling if needed

        return DummyNoSync()


class DummyEvolvableCNN(EvolvableCNN):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def forward(self, *args, **kwargs):
        return super().forward(*args, **kwargs)

    def no_sync(self):
        class DummyNoSync:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                pass  # Add cleanup or handling if needed

        return DummyNoSync()


@pytest.fixture
def mlp_actor(state_dims, action_dims):
    net = nn.Sequential(
        nn.Linear(state_dims[0][0], 64),
        nn.ReLU(),
        nn.Linear(64, action_dims[0]),
        GumbelSoftmax(),
    )
    return net


@pytest.fixture
def mlp_critic(action_dims, state_dims):
    net = nn.Sequential(
        nn.Linear(state_dims[0][0] + action_dims[0], 64), nn.ReLU(), nn.Linear(64, 1)
    )
    return net


@pytest.fixture
def cnn_actor():
    net = MultiAgentCNNActor()
    return net


@pytest.fixture
def cnn_critic():
    net = MultiAgentCNNCritic()
    return net


@pytest.fixture
def device():
    return "cuda" if torch.cuda.is_available() else "cpu"


@pytest.fixture
def mocked_accelerator():
    MagicMock(spec=Accelerator)


@pytest.fixture
def accelerated_experiences(batch_size, state_dims, action_dims, agent_ids, one_hot):
    state_size = state_dims[0]
    action_size = action_dims[0]
    if one_hot:
        states = {
            agent: torch.randint(0, state_size[0], (batch_size, 1)).float()
            for agent in agent_ids
        }
    else:
        states = {agent: torch.randn(batch_size, *state_size) for agent in agent_ids}

    actions = {agent: torch.randn(batch_size, action_size) for agent in agent_ids}
    rewards = {agent: torch.randn(batch_size, 1) for agent in agent_ids}
    dones = {agent: torch.randint(0, 2, (batch_size, 1)) for agent in agent_ids}
    if one_hot:
        next_states = {
            agent: torch.randint(0, state_size[0], (batch_size, 1)).float()
            for agent in agent_ids
        }
    else:
        next_states = {
            agent: torch.randn(batch_size, *state_size) for agent in agent_ids
        }

    return states, actions, rewards, next_states, dones


@pytest.fixture
def experiences(batch_size, state_dims, action_dims, agent_ids, one_hot, device):
    state_size = state_dims[0]
    action_size = action_dims[0]
    if one_hot:
        states = {
            agent: torch.randint(0, state_size[0], (batch_size, 1)).float().to(device)
            for agent in agent_ids
        }
    else:
        states = {
            agent: torch.randn(batch_size, *state_size).to(device)
            for agent in agent_ids
        }

    actions = {
        agent: torch.randn(batch_size, action_size).to(device) for agent in agent_ids
    }
    rewards = {agent: torch.randn(batch_size, 1).to(device) for agent in agent_ids}
    dones = {
        agent: torch.randint(0, 2, (batch_size, 1)).to(device) for agent in agent_ids
    }
    if one_hot:
        next_states = {
            agent: torch.randint(0, state_size[0], (batch_size, 1)).float().to(device)
            for agent in agent_ids
        }
    else:
        next_states = {
            agent: torch.randn(batch_size, *state_size).to(device)
            for agent in agent_ids
        }

    return states, actions, rewards, next_states, dones


@pytest.mark.parametrize(
    "net_config, accelerator_flag, state_dims",
    [
        ({"arch": "mlp", "hidden_size": [64, 64]}, False, [(4,), (4,)]),
        (
            {
                "arch": "cnn",
                "hidden_size": [8],
                "channel_size": [3],
                "kernel_size": [3],
                "stride_size": [1],
                "normalize": False,
            },
            False,
            [(3, 32, 32), (3, 32, 32)],
        ),
        (
            {
                "arch": "cnn",
                "hidden_size": [8],
                "channel_size": [3],
                "kernel_size": [3],
                "stride_size": [1],
                "normalize": False,
            },
            True,
            [(3, 32, 32), (3, 32, 32)],
        ),
    ],
)
def test_initialize_matd3_with_net_config(
    net_config, accelerator_flag, state_dims, device
):
    action_dims = [2, 2]
    one_hot = False
    n_agents = 2
    agent_ids = ["agent_0", "agent_1"]
    max_action = [(1,), (1,)]
    min_action = [(-1,), (-1,)]
    discrete_actions = False
    expl_noise = 0.1
    batch_size = 64
    policy_freq = 2
    if accelerator_flag:
        accelerator = Accelerator()
    else:
        accelerator = None
    matd3 = MATD3(
        state_dims=state_dims,
        net_config=net_config,
        action_dims=action_dims,
        one_hot=one_hot,
        n_agents=n_agents,
        agent_ids=agent_ids,
        max_action=max_action,
        min_action=min_action,
        discrete_actions=discrete_actions,
        accelerator=accelerator,
        device=device,
        policy_freq=policy_freq,
    )
    net_config.update({"output_activation": "Softmax"})
    assert matd3.state_dims == state_dims
    assert matd3.action_dims == action_dims
    assert matd3.policy_freq == policy_freq
    assert matd3.one_hot == one_hot
    assert matd3.n_agents == n_agents
    assert matd3.agent_ids == agent_ids
    assert matd3.max_action == max_action
    assert matd3.min_action == min_action
    assert matd3.discrete_actions == discrete_actions
    for noise_vec in matd3.expl_noise:
        assert torch.all(noise_vec == expl_noise)
    assert matd3.net_config == net_config, matd3.net_config
    assert matd3.batch_size == batch_size
    assert matd3.multi
    assert matd3.total_state_dims == sum(state[0] for state in state_dims)
    assert matd3.total_actions == sum(action_dims)
    assert matd3.scores == []
    assert matd3.fitness == []
    assert matd3.steps == [0]
    assert matd3.actor_networks is None
    assert matd3.critic_networks is None
    if net_config["arch"] == "mlp":
        evo_type = EvolvableMLP
        assert matd3.arch == "mlp"
    else:
        evo_type = EvolvableCNN
        assert matd3.arch == "cnn"
    assert all(isinstance(actor, evo_type) for actor in matd3.actors)
    assert all(isinstance(critic_1, evo_type) for critic_1 in matd3.critics_1)
    assert all(isinstance(critic_2, evo_type) for critic_2 in matd3.critics_2)
    assert all(
        isinstance(actor_target, evo_type) for actor_target in matd3.actor_targets
    )
    assert all(
        isinstance(critic_target_1, evo_type)
        for critic_target_1 in matd3.critic_targets_1
    )
    assert all(
        isinstance(critic_target_2, evo_type)
        for critic_target_2 in matd3.critic_targets_2
    )
    if accelerator is None:
        assert all(
            isinstance(actor_optimizer, optim.Adam)
            for actor_optimizer in matd3.actor_optimizers
        )
        assert all(
            isinstance(critic_1_optimizer, optim.Adam)
            for critic_1_optimizer in matd3.critic_1_optimizers
        )
        assert all(
            isinstance(critic_2_optimizer, optim.Adam)
            for critic_2_optimizer in matd3.critic_2_optimizers
        )
    else:
        assert all(
            isinstance(actor_optimizer, AcceleratedOptimizer)
            for actor_optimizer in matd3.actor_optimizers
        )
        assert all(
            isinstance(critic_1_optimizer, AcceleratedOptimizer)
            for critic_1_optimizer in matd3.critic_1_optimizers
        )
        assert all(
            isinstance(critic_2_optimizer, AcceleratedOptimizer)
            for critic_2_optimizer in matd3.critic_2_optimizers
        )
    assert isinstance(matd3.criterion, nn.MSELoss)


@pytest.mark.parametrize(
    "state_dims, action_dims, accelerator_flag",
    [
        ([(6,) for _ in range(2)], [2 for _ in range(2)], False),
        ([(6,) for _ in range(2)], [2 for _ in range(2)], True),
    ],
)
def test_initialize_matd3_with_mlp_networks(
    mlp_actor, mlp_critic, state_dims, action_dims, accelerator_flag, device
):
    if accelerator_flag:
        accelerator = Accelerator()
    else:
        accelerator = None
    evo_actors = [
        MakeEvolvable(network=mlp_actor, input_tensor=torch.randn(1, 6), device=device)
        for _ in range(2)
    ]
    evo_critics_1 = [
        MakeEvolvable(network=mlp_critic, input_tensor=torch.randn(1, 8), device=device)
        for _ in range(2)
    ]
    evo_critics_2 = [
        MakeEvolvable(network=mlp_critic, input_tensor=torch.randn(1, 8), device=device)
        for _ in range(2)
    ]
    evo_critics = [evo_critics_1, evo_critics_2]
    matd3 = MATD3(
        state_dims=state_dims,
        action_dims=action_dims,
        one_hot=False,
        agent_ids=["agent_0", "agent_1"],
        n_agents=len(state_dims),
        max_action=[(1,), (1,)],
        min_action=[(-1,), (-1,)],
        discrete_actions=True,
        actor_networks=evo_actors,
        critic_networks=evo_critics,
        device=device,
        accelerator=accelerator,
        policy_freq=2,
    )
    assert all(isinstance(actor, MakeEvolvable) for actor in matd3.actors)
    assert all(isinstance(critic_1, MakeEvolvable) for critic_1 in matd3.critics_1)
    assert all(isinstance(critic_2, MakeEvolvable) for critic_2 in matd3.critics_2)
    assert matd3.net_config is None
    assert matd3.arch == "mlp"
    assert matd3.state_dims == state_dims
    assert matd3.action_dims == action_dims
    assert matd3.one_hot is False
    assert matd3.n_agents == 2
    assert matd3.policy_freq == 2
    assert matd3.agent_ids == ["agent_0", "agent_1"]
    assert matd3.max_action == [(1,), (1,)]
    assert matd3.min_action == [(-1,), (-1,)]
    assert matd3.discrete_actions is True
    assert matd3.multi
    assert matd3.total_state_dims == sum(state[0] for state in state_dims)
    assert matd3.total_actions == sum(action_dims)
    assert matd3.scores == []
    assert matd3.fitness == []
    assert matd3.steps == [0]
    if accelerator is None:
        assert all(
            isinstance(actor_optimizer, optim.Adam)
            for actor_optimizer in matd3.actor_optimizers
        )
        assert all(
            isinstance(critic_1_optimizer, optim.Adam)
            for critic_1_optimizer in matd3.critic_1_optimizers
        )
        assert all(
            isinstance(critic_2_optimizer, optim.Adam)
            for critic_2_optimizer in matd3.critic_2_optimizers
        )
    else:
        assert all(
            isinstance(actor_optimizer, AcceleratedOptimizer)
            for actor_optimizer in matd3.actor_optimizers
        )
        assert all(
            isinstance(critic_1_optimizer, AcceleratedOptimizer)
            for critic_1_optimizer in matd3.critic_1_optimizers
        )
        assert all(
            isinstance(critic_2_optimizer, AcceleratedOptimizer)
            for critic_2_optimizer in matd3.critic_2_optimizers
        )
    assert isinstance(matd3.criterion, nn.MSELoss)


@pytest.mark.parametrize(
    "state_dims, action_dims, accelerator_flag",
    [
        ([(4, 210, 160) for _ in range(2)], [2 for _ in range(2)], False),
        ([(4, 210, 160) for _ in range(2)], [2 for _ in range(2)], True),
    ],
)
def test_initialize_matd3_with_cnn_networks(
    cnn_actor, cnn_critic, state_dims, action_dims, accelerator_flag, device
):
    if accelerator_flag:
        accelerator = Accelerator()
    else:
        accelerator = None
    evo_actors = [
        MakeEvolvable(
            network=cnn_actor,
            input_tensor=torch.randn(1, 4, 2, 210, 160),
            device=device,
        )
        for _ in range(2)
    ]
    evo_critics_1 = [
        MakeEvolvable(
            network=cnn_critic,
            input_tensor=torch.randn(1, 4, 2, 210, 160),
            secondary_input_tensor=torch.randn(1, 2),
            device=device,
        )
        for _ in range(2)
    ]
    evo_critics_2 = [
        MakeEvolvable(
            network=cnn_critic,
            input_tensor=torch.randn(1, 4, 2, 210, 160),
            secondary_input_tensor=torch.randn(1, 2),
            device=device,
        )
        for _ in range(2)
    ]
    evo_critics = [evo_critics_1, evo_critics_2]
    matd3 = MATD3(
        state_dims=state_dims,
        action_dims=action_dims,
        one_hot=False,
        agent_ids=["agent_0", "agent_1"],
        n_agents=len(state_dims),
        max_action=[(1,), (1,)],
        min_action=[(-1,), (-1,)],
        discrete_actions=True,
        actor_networks=evo_actors,
        critic_networks=evo_critics,
        device=device,
        accelerator=accelerator,
        policy_freq=2,
    )
    assert all(isinstance(actor, MakeEvolvable) for actor in matd3.actors)
    assert all(isinstance(critic_1, MakeEvolvable) for critic_1 in matd3.critics_1)
    assert all(isinstance(critic_2, MakeEvolvable) for critic_2 in matd3.critics_2)
    assert matd3.net_config is None
    assert matd3.arch == "cnn"
    assert matd3.state_dims == state_dims
    assert matd3.policy_freq == 2
    assert matd3.action_dims == action_dims
    assert matd3.one_hot is False
    assert matd3.n_agents == 2
    assert matd3.agent_ids == ["agent_0", "agent_1"]
    assert matd3.max_action == [(1,), (1,)]
    assert matd3.min_action == [(-1,), (-1,)]
    assert matd3.discrete_actions is True
    assert matd3.multi
    assert matd3.total_state_dims == sum(state[0] for state in state_dims)
    assert matd3.total_actions == sum(action_dims)
    assert matd3.scores == []
    assert matd3.fitness == []
    assert matd3.steps == [0]
    if accelerator is None:
        assert all(
            isinstance(actor_optimizer, optim.Adam)
            for actor_optimizer in matd3.actor_optimizers
        )
        assert all(
            isinstance(critic_1_optimizer, optim.Adam)
            for critic_1_optimizer in matd3.critic_1_optimizers
        )
        assert all(
            isinstance(critic_2_optimizer, optim.Adam)
            for critic_2_optimizer in matd3.critic_2_optimizers
        )
    else:
        assert all(
            isinstance(actor_optimizer, AcceleratedOptimizer)
            for actor_optimizer in matd3.actor_optimizers
        )
        assert all(
            isinstance(critic_1_optimizer, AcceleratedOptimizer)
            for critic_1_optimizer in matd3.critic_1_optimizers
        )
        assert all(
            isinstance(critic_2_optimizer, AcceleratedOptimizer)
            for critic_2_optimizer in matd3.critic_2_optimizers
        )
    assert isinstance(matd3.criterion, nn.MSELoss)


@pytest.mark.parametrize(
    "state_dims, action_dims, net",
    [
        ([[4] for _ in range(2)], [2 for _ in range(2)], "mlp"),
        ([(4, 210, 160) for _ in range(2)], [2 for _ in range(2)], "cnn"),
    ],
)
def test_initialize_matd3_with_evo_networks(state_dims, action_dims, net, device):
    if net == "mlp":
        evo_actors = [
            EvolvableMLP(
                num_inputs=state_dims[x][0],
                num_outputs=action_dims[x],
                hidden_size=[64, 64],
                mlp_activation="ReLU",
                mlp_output_activation="Tanh",
            )
            for x in range(2)
        ]
        evo_critics = [
            [
                EvolvableMLP(
                    num_inputs=sum(state_dim[0] for state_dim in state_dims)
                    + sum(action_dims),
                    num_outputs=1,
                    hidden_size=[64, 64],
                    mlp_activation="ReLU",
                )
                for x in range(2)
            ]
            for _ in range(2)
        ]
    else:
        evo_actors = [
            EvolvableCNN(
                input_shape=state_dims[0],
                num_actions=action_dims[0],
                channel_size=[8, 8],
                kernel_size=[2, 2],
                stride_size=[1, 1],
                hidden_size=[64, 64],
                mlp_activation="ReLU",
                multi=True,
                n_agents=2,
                mlp_output_activation="Tanh",
            )
            for _ in range(2)
        ]
        evo_critics = [
            [
                EvolvableCNN(
                    input_shape=state_dims[0],
                    num_actions=sum(action_dims),
                    channel_size=[8, 8],
                    kernel_size=[2, 2],
                    stride_size=[1, 1],
                    hidden_size=[64, 64],
                    n_agents=2,
                    critic=True,
                    multi=True,
                    mlp_activation="ReLU",
                )
                for _ in range(2)
            ]
            for _ in range(2)
        ]
    matd3 = MATD3(
        state_dims=state_dims,
        action_dims=action_dims,
        one_hot=False,
        agent_ids=["agent_0", "agent_1"],
        n_agents=len(state_dims),
        max_action=[(1,), (1,)],
        min_action=[(-1,), (-1,)],
        discrete_actions=True,
        actor_networks=evo_actors,
        critic_networks=evo_critics,
        device=device,
    )
    assert all(
        isinstance(actor, (EvolvableMLP, EvolvableCNN)) for actor in matd3.actors
    )
    assert all(
        isinstance(critic, (EvolvableMLP, EvolvableCNN)) for critic in matd3.critics_1
    )
    assert all(
        isinstance(critic, (EvolvableMLP, EvolvableCNN)) for critic in matd3.critics_2
    )
    if net == "mlp":
        assert matd3.arch == "mlp"
    else:
        assert matd3.arch == "cnn"
    assert matd3.state_dims == state_dims
    assert matd3.policy_freq == 2
    assert matd3.action_dims == action_dims
    assert matd3.one_hot is False
    assert matd3.n_agents == 2
    assert matd3.agent_ids == ["agent_0", "agent_1"]
    assert matd3.max_action == [(1,), (1,)]
    assert matd3.min_action == [(-1,), (-1,)]
    assert matd3.discrete_actions is True
    assert matd3.multi
    assert matd3.total_state_dims == sum(state[0] for state in state_dims)
    assert matd3.total_actions == sum(action_dims)
    assert matd3.scores == []
    assert matd3.fitness == []
    assert matd3.steps == [0]
    assert all(
        isinstance(actor_optimizer, optim.Adam)
        for actor_optimizer in matd3.actor_optimizers
    )
    assert all(
        isinstance(critic_1_optimizer, optim.Adam)
        for critic_1_optimizer in matd3.critic_1_optimizers
    )
    assert all(
        isinstance(critic_2_optimizer, optim.Adam)
        for critic_2_optimizer in matd3.critic_2_optimizers
    )

    assert isinstance(matd3.criterion, nn.MSELoss)


@pytest.mark.parametrize(
    "state_dims, action_dims",
    [
        ([[4] for _ in range(2)], [2 for _ in range(2)]),
    ],
)
def test_initialize_matd3_with_incorrect_evo_networks(state_dims, action_dims, device):
    evo_actors = []
    evo_critics = []

    with pytest.raises(AssertionError):
        matd3 = MATD3(
            state_dims=state_dims,
            action_dims=action_dims,
            one_hot=False,
            agent_ids=["agent_0", "agent_1"],
            n_agents=len(state_dims),
            max_action=[(1,), (1,)],
            min_action=[(-1,), (-1,)],
            discrete_actions=True,
            actor_networks=evo_actors,
            critic_networks=evo_critics,
        )
        assert matd3


@pytest.mark.parametrize(
    "state_dims, action_dims",
    [
        ([(6,) for _ in range(2)], [2 for _ in range(2)]),
    ],
)
def test_matd3_init_warning(mlp_actor, state_dims, action_dims, device):
    warning_string = "Actor and critic network lists must both be supplied to use custom networks. Defaulting to net config."
    evo_actors = [
        MakeEvolvable(network=mlp_actor, input_tensor=torch.randn(1, 6), device=device)
        for _ in range(2)
    ]
    with pytest.warns(UserWarning, match=warning_string):
        MATD3(
            state_dims=state_dims,
            action_dims=action_dims,
            one_hot=False,
            agent_ids=["agent_0", "agent_1"],
            n_agents=len(state_dims),
            max_action=[(1,), (1,)],
            min_action=[(-1,), (-1,)],
            discrete_actions=True,
            actor_networks=evo_actors,
            device=device,
        )


@pytest.mark.parametrize(
    "mode", (None, 0, False, "default", "reduce-overhead", "max-autotune")
)
def test_matd3_init_with_compile_no_error(mode):
    matd3 = MATD3(
        state_dims=[(1,), (1,)],
        action_dims=[1, 1],
        one_hot=False,
        agent_ids=["agent_0", "agent_1"],
        n_agents=2,
        max_action=[(1,), (1,)],
        min_action=[(-1,), (-1,)],
        discrete_actions=False,
        device="cuda" if torch.cuda.is_available() else "cpu",
        torch_compiler=mode,
    )
    if isinstance(mode, str):
        assert all(
            isinstance(a, torch._dynamo.eval_frame.OptimizedModule)
            for a in matd3.actors
        )
        assert all(
            isinstance(a, torch._dynamo.eval_frame.OptimizedModule)
            for a in matd3.actor_targets
        )
        assert all(
            isinstance(a, torch._dynamo.eval_frame.OptimizedModule)
            for a in matd3.critics_1
        )
        assert all(
            isinstance(a, torch._dynamo.eval_frame.OptimizedModule)
            for a in matd3.critics_2
        )
        assert all(
            isinstance(a, torch._dynamo.eval_frame.OptimizedModule)
            for a in matd3.critic_targets_1
        )
        assert all(
            isinstance(a, torch._dynamo.eval_frame.OptimizedModule)
            for a in matd3.critic_targets_2
        )
        assert matd3.torch_compiler == mode
    else:
        assert isinstance(matd3, MATD3)


@pytest.mark.parametrize("mode", (1, True, "max-autotune-no-cudagraphs"))
def test_matd3_init_with_compile_error(mode):
    err_string = (
        "Choose between torch compiler modes: "
        "default, reduce-overhead, max-autotune or None"
    )
    with pytest.raises(AssertionError, match=err_string):
        MATD3(
            state_dims=[(1,), (1,)],
            action_dims=[1, 1],
            one_hot=False,
            agent_ids=["agent_0", "agent_1"],
            n_agents=2,
            max_action=[(1,), (1,)],
            min_action=[(-1,), (-1,)],
            discrete_actions=False,
            device="cuda" if torch.cuda.is_available() else "cpu",
            torch_compiler=mode,
        )


@pytest.mark.parametrize(
    "training, state_dims, action_dims, discrete_actions, one_hot, compile",
    [
        (1, [(6,) for _ in range(2)], [2 for _ in range(2)], False, False, False),
        (0, [(6,) for _ in range(2)], [2 for _ in range(2)], False, False, False),
        (1, [(6,) for _ in range(2)], [2 for _ in range(2)], True, False, False),
        (0, [(6,) for _ in range(2)], [2 for _ in range(2)], True, False, False),
        (1, [(6,) for _ in range(2)], [2 for _ in range(2)], False, True, False),
        (0, [(6,) for _ in range(2)], [2 for _ in range(2)], False, True, True),
        (1, [(6,) for _ in range(2)], [2 for _ in range(2)], True, True, True),
        (0, [(6,) for _ in range(2)], [2 for _ in range(2)], False, True, False),
        (1, [(6,) for _ in range(2)], [2 for _ in range(2)], True, True, False),
        (0, [(6,) for _ in range(2)], [2 for _ in range(2)], True, True, False),
    ],
)
def test_matd3_get_action_mlp(
    training, state_dims, action_dims, discrete_actions, one_hot, compile, device
):
    agent_ids = ["agent_0", "agent_1"]
    if one_hot:
        state = {
            agent: np.random.randint(0, state_dims[idx], 1)
            for idx, agent in enumerate(agent_ids)
        }
    else:
        state = {
            agent: np.random.randn(*state_dims[idx])
            for idx, agent in enumerate(agent_ids)
        }

    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=one_hot,
        net_config={"arch": "mlp", "hidden_size": [8, 8]},
        n_agents=2,
        agent_ids=agent_ids,
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=discrete_actions,
        device=device,
        torch_compiler="default" if compile else None,
    )
    cont_actions, discrete_action = matd3.get_action(state, training)
    for idx, env_actions in enumerate(list(cont_actions.values())):
        for action in env_actions:
            assert len(action) == action_dims[idx]
            if discrete_actions:
                assert (
                    np.isclose(
                        sum(action),
                        1.0,
                        atol=0.1,
                        rtol=1e-3,
                    ).all()
                    or np.isclose(
                        sum(action),
                        0.0,
                        atol=0.1,
                        rtol=1e-3,
                    ).all()
                )
            act = action[idx]
            assert act.dtype == np.float32
            assert -1 <= act.all() <= 1

    if discrete_actions:
        for idx, env_action in enumerate(list(discrete_action.values())):
            for action in env_action:
                assert action <= action_dims[idx] - 1
    matd3 = None


@pytest.mark.parametrize(
    "training, state_dims, action_dims, discrete_actions, compile",
    [
        (1, [(3, 32, 32) for _ in range(2)], [2 for _ in range(2)], False, False),
        (0, [(3, 32, 32) for _ in range(2)], [2 for _ in range(2)], False, False),
        (1, [(3, 32, 32) for _ in range(2)], [2 for _ in range(2)], True, False),
        (1, [(3, 32, 32) for _ in range(2)], [2 for _ in range(2)], True, True),
        (0, [(3, 32, 32) for _ in range(2)], [2 for _ in range(2)], True, False),
    ],
)
def test_matd3_get_action_cnn(
    training, state_dims, action_dims, discrete_actions, compile, device
):
    agent_ids = ["agent_0", "agent_1"]
    net_config = {
        "arch": "cnn",
        "hidden_size": [64, 64],
        "channel_size": [16],
        "kernel_size": [3],
        "stride_size": [1],
        "normalize": False,
    }
    state = {
        agent: np.random.randn(*state_dims[idx]) for idx, agent in enumerate(agent_ids)
    }
    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=False,
        n_agents=2,
        agent_ids=agent_ids,
        net_config=net_config,
        max_action=[[1], [1]],
        min_action=[[-1], [0]],
        discrete_actions=discrete_actions,
        device=device,
        torch_compiler="default" if compile else None,
    )
    cont_actions, discrete_action = matd3.get_action(state, training)
    for idx, env_actions in enumerate(list(cont_actions.values())):
        for action in env_actions:
            assert len(action) == action_dims[idx]
            if discrete_actions:
                assert (
                    np.isclose(
                        sum(action),
                        1.0,
                        atol=0.1,
                        rtol=1e-3,
                    ).all()
                    or np.isclose(
                        sum(action),
                        0.0,
                        atol=0.1,
                        rtol=1e-3,
                    ).all()
                )
            act = action[idx]
            assert act.dtype == np.float32
            assert -1 <= act.all() <= 1

    if discrete_actions:
        for idx, env_action in enumerate(list(discrete_action.values())):
            for action in env_action:
                assert action <= action_dims[idx] - 1


@pytest.mark.parametrize(
    "training, state_dims, action_dims, discrete_actions, compile",
    [
        (1, [(6,) for _ in range(2)], [2 for _ in range(2)], True, False),
        (0, [(6,) for _ in range(2)], [2 for _ in range(2)], True, False),
        (1, [(6,) for _ in range(2)], [2 for _ in range(2)], False, False),
        (1, [(6,) for _ in range(2)], [2 for _ in range(2)], False, True),
        (0, [(6,) for _ in range(2)], [2 for _ in range(2)], False, False),
    ],
)
def test_matd3_get_action_distributed(
    training, state_dims, action_dims, discrete_actions, compile
):
    accelerator = Accelerator()
    agent_ids = ["agent_0", "agent_1"]
    state = {
        agent: np.random.randn(*state_dims[idx]) for idx, agent in enumerate(agent_ids)
    }
    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=False,
        n_agents=2,
        agent_ids=agent_ids,
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=discrete_actions,
        accelerator=accelerator,
        torch_compiler="default" if compile else None,
    )
    new_actors = [
        DummyEvolvableMLP(
            num_inputs=actor.num_inputs,
            num_outputs=actor.num_outputs,
            hidden_size=actor.hidden_size,
            device=actor.device,
            mlp_output_activation=actor.mlp_output_activation,
        )
        for actor in matd3.actors
    ]
    matd3.actors = new_actors
    cont_actions, discrete_action = matd3.get_action(state, training)
    for idx, env_actions in enumerate(list(cont_actions.values())):
        for action in env_actions:
            assert len(action) == action_dims[idx]
            if discrete_actions:
                assert (
                    np.isclose(
                        sum(action),
                        1.0,
                        atol=0.1,
                        rtol=1e-3,
                    ).all()
                    or np.isclose(
                        sum(action),
                        0.0,
                        atol=0.1,
                        rtol=1e-3,
                    ).all()
                )
            act = action[idx]
            assert act.dtype == np.float32
            assert -1 <= act.all() <= 1

    if discrete_actions:
        for idx, env_action in enumerate(list(discrete_action.values())):
            for action in env_action:
                assert action <= action_dims[idx] - 1


@pytest.mark.parametrize(
    "training, state_dims, action_dims, discrete_actions, compile",
    [
        (1, [(6,) for _ in range(2)], [2 for _ in range(2)], False, False),
        (0, [(6,) for _ in range(2)], [2 for _ in range(2)], False, False),
        (1, [(6,) for _ in range(2)], [2 for _ in range(2)], True, False),
        (0, [(6,) for _ in range(2)], [2 for _ in range(2)], True, False),
        (0, [(6,) for _ in range(2)], [2 for _ in range(2)], True, True),
    ],
)
def test_matd3_get_action_agent_masking(
    training, state_dims, action_dims, discrete_actions, compile, device
):
    agent_ids = ["agent_0", "agent_1"]
    state = {
        agent: np.random.randn(*state_dims[idx]) for idx, agent in enumerate(agent_ids)
    }
    agent_mask = {"agent_0": False, "agent_1": True}
    if discrete_actions:
        env_defined_actions = {"agent_0": 1, "agent_1": None}
    else:
        env_defined_actions = {"agent_0": np.array([0, 1]), "agent_1": None}
    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=False,
        n_agents=2,
        agent_ids=agent_ids,
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=discrete_actions,
        device=device,
        torch_compiler="default" if compile else None,
    )
    cont_actions, discrete_action = matd3.get_action(
        state, training, agent_mask=agent_mask, env_defined_actions=env_defined_actions
    )
    if discrete_actions:
        assert np.array_equal(discrete_action["agent_0"], 1), discrete_action["agent_0"]
    assert np.array_equal(cont_actions["agent_0"], np.array([0, 1])), cont_actions[
        "agent_0"
    ]


@pytest.mark.parametrize(
    "state_dims, discrete_actions, batch_size, action_dims, agent_ids, one_hot, compile",
    [
        ([(6,), (6,)], False, 64, [2, 2], ["agent_0", "agent_1"], False, False),
        ([(6,), (6,)], False, 64, [2, 2], ["agent_0", "agent_1"], True, False),
        ([(6,), (6,)], True, 64, [2, 2], ["agent_0", "agent_1"], False, False),
        ([(6,), (6,)], True, 64, [2, 2], ["agent_0", "agent_1"], True, False),
        ([(6,), (6,)], False, 64, [2, 2], ["agent_0", "agent_1"], True, True),
        ([(6,), (6,)], True, 64, [2, 2], ["agent_0", "agent_1"], False, True),
    ],
)
def test_matd3_learns_from_experiences_mlp(
    state_dims,
    experiences,
    discrete_actions,
    batch_size,
    action_dims,
    agent_ids,
    one_hot,
    compile,
    device,
):
    action_dims = [2, 2]
    agent_ids = ["agent_0", "agent_1"]
    policy_freq = 2
    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=one_hot,
        n_agents=2,
        agent_ids=agent_ids,
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=discrete_actions,
        device=device,
        policy_freq=policy_freq,
        torch_compiler="default" if compile else None,
    )
    actors = matd3.actors
    actor_targets = matd3.actor_targets
    actors_pre_learn_sd = [copy.deepcopy(actor.state_dict()) for actor in matd3.actors]
    critics_1 = matd3.critics_1
    critic_targets_1 = matd3.critic_targets_1
    critics_2 = matd3.critics_2
    critic_targets_2 = matd3.critic_targets_2
    critics_1_pre_learn_sd = [
        str(copy.deepcopy(critic_1.state_dict())) for critic_1 in matd3.critics_1
    ]
    critics_2_pre_learn_sd = [
        str(copy.deepcopy(critic_2.state_dict())) for critic_2 in matd3.critics_2
    ]

    for _ in range(4 * policy_freq):
        matd3.scores.append(0)
        loss = matd3.learn(experiences)

    assert isinstance(loss, dict)
    for agent_id in matd3.agent_ids:
        assert loss[agent_id][-1] >= 0.0
    for old_actor, updated_actor in zip(actors, matd3.actors):
        assert old_actor == updated_actor
    for old_actor_target, updated_actor_target in zip(
        actor_targets, matd3.actor_targets
    ):
        assert old_actor_target == updated_actor_target
    for old_actor_state_dict, updated_actor in zip(actors_pre_learn_sd, matd3.actors):
        assert old_actor_state_dict != str(updated_actor.state_dict())
    for old_critic_1, updated_critic_1 in zip(critics_1, matd3.critics_1):
        assert old_critic_1 == updated_critic_1
    for old_critic_target_1, updated_critic_target_1 in zip(
        critic_targets_1, matd3.critic_targets_1
    ):
        assert old_critic_target_1 == updated_critic_target_1
    for old_critic_1_state_dict, updated_critic_1 in zip(
        critics_1_pre_learn_sd, matd3.critics_1
    ):
        assert old_critic_1_state_dict != str(updated_critic_1.state_dict())
    for old_critic_2, updated_critic_2 in zip(critics_2, matd3.critics_2):
        assert old_critic_2 == updated_critic_2
    for old_critic_target_2, updated_critic_target_2 in zip(
        critic_targets_2, matd3.critic_targets_2
    ):
        assert old_critic_target_2 == updated_critic_target_2
    for old_critic_2_state_dict, updated_critic_2 in zip(
        critics_2_pre_learn_sd, matd3.critics_2
    ):
        assert old_critic_2_state_dict != str(updated_critic_2.state_dict())


def no_sync(self):
    class DummyNoSync:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass  # Add cleanup or handling if needed

    return DummyNoSync()


@pytest.mark.parametrize(
    "state_dims, discrete_actions, batch_size, action_dims, agent_ids, one_hot, compile",
    [
        ([(6,), (6,)], False, 64, [2, 2], ["agent_0", "agent_1"], False, False),
        ([(6,), (6,)], False, 64, [2, 2], ["agent_0", "agent_1"], True, False),
        ([(6,), (6,)], True, 64, [2, 2], ["agent_0", "agent_1"], False, False),
        ([(6,), (6,)], True, 64, [2, 2], ["agent_0", "agent_1"], True, False),
        ([(6,), (6,)], False, 64, [2, 2], ["agent_0", "agent_1"], False, True),
        ([(6,), (6,)], True, 64, [2, 2], ["agent_0", "agent_1"], True, True),
    ],
)
def test_matd3_learns_from_experiences_mlp_distributed(
    state_dims,
    accelerated_experiences,
    discrete_actions,
    batch_size,
    action_dims,
    agent_ids,
    one_hot,
    compile,
):
    accelerator = Accelerator(device_placement=False)
    action_dims = [2, 2]
    agent_ids = ["agent_0", "agent_1"]
    policy_freq = 2
    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=one_hot,
        n_agents=2,
        agent_ids=agent_ids,
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=discrete_actions,
        accelerator=accelerator,
        policy_freq=policy_freq,
        torch_compiler="default" if compile else None,
    )

    for (
        actor,
        critic_1,
        critic_2,
        actor_target,
        critic_target_1,
        critic_target_2,
    ) in zip(
        matd3.actors,
        matd3.critics_1,
        matd3.critics_2,
        matd3.actor_targets,
        matd3.critic_targets_1,
        matd3.critic_targets_2,
    ):
        actor.no_sync = no_sync.__get__(actor)
        critic_1.no_sync = no_sync.__get__(critic_1)
        critic_2.no_sync = no_sync.__get__(critic_2)
        actor_target.no_sync = no_sync.__get__(actor_target)
        critic_target_1.no_sync = no_sync.__get__(critic_target_1)
        critic_target_2.no_sync = no_sync.__get__(critic_target_2)

    actors = matd3.actors
    actor_targets = matd3.actor_targets
    actors_pre_learn_sd = [copy.deepcopy(actor.state_dict()) for actor in matd3.actors]
    critics_1 = matd3.critics_1
    critic_targets_1 = matd3.critic_targets_1
    critics_2 = matd3.critics_2
    critic_targets_2 = matd3.critic_targets_2
    critics_1_pre_learn_sd = [
        str(copy.deepcopy(critic_1.state_dict())) for critic_1 in matd3.critics_1
    ]
    critics_2_pre_learn_sd = [
        str(copy.deepcopy(critic_2.state_dict())) for critic_2 in matd3.critics_2
    ]

    for _ in range(4 * policy_freq):
        matd3.scores.append(0)
        loss = matd3.learn(accelerated_experiences)

    assert isinstance(loss, dict)
    for agent_id in matd3.agent_ids:
        assert loss[agent_id][-1] >= 0.0
    for old_actor, updated_actor in zip(actors, matd3.actors):
        assert old_actor == updated_actor
    for old_actor_target, updated_actor_target in zip(
        actor_targets, matd3.actor_targets
    ):
        assert old_actor_target == updated_actor_target
    for old_actor_state_dict, updated_actor in zip(actors_pre_learn_sd, matd3.actors):
        assert old_actor_state_dict != str(updated_actor.state_dict())
    for old_critic_1, updated_critic_1 in zip(critics_1, matd3.critics_1):
        assert old_critic_1 == updated_critic_1
    for old_critic_target_1, updated_critic_target_1 in zip(
        critic_targets_1, matd3.critic_targets_1
    ):
        assert old_critic_target_1 == updated_critic_target_1
    for old_critic_1_state_dict, updated_critic_1 in zip(
        critics_1_pre_learn_sd, matd3.critics_1
    ):
        assert old_critic_1_state_dict != str(updated_critic_1.state_dict())
    for old_critic_2, updated_critic_2 in zip(critics_2, matd3.critics_2):
        assert old_critic_2 == updated_critic_2
    for old_critic_target_2, updated_critic_target_2 in zip(
        critic_targets_2, matd3.critic_targets_2
    ):
        assert old_critic_target_2 == updated_critic_target_2
    for old_critic_2_state_dict, updated_critic_2 in zip(
        critics_2_pre_learn_sd, matd3.critics_2
    ):
        assert old_critic_2_state_dict != str(updated_critic_2.state_dict())


@pytest.mark.parametrize(
    "state_dims, discrete_actions, batch_size, action_dims, agent_ids, one_hot, compile",
    [
        (
            [(3, 32, 32), (3, 32, 32)],
            False,
            64,
            [2, 2],
            ["agent_0", "agent_1"],
            False,
            False,
        ),
        (
            [(3, 32, 32), (3, 32, 32)],
            True,
            64,
            [2, 2],
            ["agent_0", "agent_1"],
            False,
            False,
        ),
        (
            [(3, 32, 32), (3, 32, 32)],
            True,
            64,
            [2, 2],
            ["agent_0", "agent_1"],
            False,
            True,
        ),
    ],
)
def test_matd3_learns_from_experiences_cnn(
    state_dims,
    experiences,
    discrete_actions,
    batch_size,
    action_dims,
    agent_ids,
    one_hot,
    compile,
    device,
):
    action_dims = [2, 2]
    agent_ids = ["agent_0", "agent_1"]
    policy_freq = 2
    net_config = {
        "arch": "cnn",
        "hidden_size": [8],
        "channel_size": [16],
        "kernel_size": [3],
        "stride_size": [1],
        "normalize": False,
    }
    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=one_hot,
        n_agents=2,
        net_config=net_config,
        agent_ids=agent_ids,
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=discrete_actions,
        device=device,
        policy_freq=policy_freq,
        torch_compiler="default" if compile else None,
    )
    actors = matd3.actors
    actor_targets = matd3.actor_targets
    actors_pre_learn_sd = [copy.deepcopy(actor.state_dict()) for actor in matd3.actors]
    critics_1 = matd3.critics_1
    critic_targets_1 = matd3.critic_targets_1
    critics_2 = matd3.critics_2
    critic_targets_2 = matd3.critic_targets_2
    critics_1_pre_learn_sd = [
        str(copy.deepcopy(critic_1.state_dict())) for critic_1 in matd3.critics_1
    ]
    critics_2_pre_learn_sd = [
        str(copy.deepcopy(critic_2.state_dict())) for critic_2 in matd3.critics_2
    ]

    for _ in range(100 * policy_freq):
        matd3.scores.append(0)
        loss = matd3.learn(experiences)

    assert isinstance(loss, dict)
    for agent_id in matd3.agent_ids:
        assert loss[agent_id][-1] >= 0.0
    for old_actor, updated_actor in zip(actors, matd3.actors):
        assert old_actor == updated_actor
    for old_actor_target, updated_actor_target in zip(
        actor_targets, matd3.actor_targets
    ):
        assert old_actor_target == updated_actor_target
    for old_actor_state_dict, updated_actor in zip(actors_pre_learn_sd, matd3.actors):
        assert old_actor_state_dict != str(updated_actor.state_dict())
    for old_critic_1, updated_critic_1 in zip(critics_1, matd3.critics_1):
        assert old_critic_1 == updated_critic_1
    for old_critic_target_1, updated_critic_target_1 in zip(
        critic_targets_1, matd3.critic_targets_1
    ):
        assert old_critic_target_1 == updated_critic_target_1
    for old_critic_1_state_dict, updated_critic_1 in zip(
        critics_1_pre_learn_sd, matd3.critics_1
    ):
        assert old_critic_1_state_dict != str(updated_critic_1.state_dict())
    for old_critic_2, updated_critic_2 in zip(critics_2, matd3.critics_2):
        assert old_critic_2 == updated_critic_2
    for old_critic_target_2, updated_critic_target_2 in zip(
        critic_targets_2, matd3.critic_targets_2
    ):
        assert old_critic_target_2 == updated_critic_target_2
    for old_critic_2_state_dict, updated_critic_2 in zip(
        critics_2_pre_learn_sd, matd3.critics_2
    ):
        assert old_critic_2_state_dict != str(updated_critic_2.state_dict())


# @pytest.mark.skip
@pytest.mark.parametrize(
    "state_dims, discrete_actions, batch_size, action_dims, agent_ids, one_hot, compile",
    [
        (
            [(3, 32, 32), (3, 32, 32)],
            False,
            64,
            [2, 2],
            ["agent_0", "agent_1"],
            False,
            False,
        ),
        (
            [(3, 32, 32), (3, 32, 32)],
            True,
            64,
            [2, 2],
            ["agent_0", "agent_1"],
            False,
            False,
        ),
        (
            [(3, 32, 32), (3, 32, 32)],
            False,
            64,
            [2, 2],
            ["agent_0", "agent_1"],
            False,
            True,
        ),
    ],
)
def test_matd3_learns_from_experiences_cnn_distributed(
    state_dims,
    accelerated_experiences,
    discrete_actions,
    batch_size,
    action_dims,
    agent_ids,
    one_hot,
    compile,
    device,
):
    accelerator = Accelerator(device_placement=False)
    action_dims = [2, 2]
    agent_ids = ["agent_0", "agent_1"]
    net_config = {
        "arch": "cnn",
        "hidden_size": [8],
        "channel_size": [16],
        "kernel_size": [3],
        "stride_size": [1],
        "normalize": False,
    }
    policy_freq = 2
    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=one_hot,
        n_agents=2,
        net_config=net_config,
        agent_ids=agent_ids,
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=discrete_actions,
        accelerator=accelerator,
        policy_freq=policy_freq,
        torch_compiler="default" if compile else None,
    )

    for (
        actor,
        critic_1,
        critic_2,
        actor_target,
        critic_target_1,
        critic_target_2,
    ) in zip(
        matd3.actors,
        matd3.critics_1,
        matd3.critics_2,
        matd3.actor_targets,
        matd3.critic_targets_1,
        matd3.critic_targets_2,
    ):
        actor.no_sync = no_sync.__get__(actor)
        critic_1.no_sync = no_sync.__get__(critic_1)
        critic_2.no_sync = no_sync.__get__(critic_2)
        actor_target.no_sync = no_sync.__get__(actor_target)
        critic_target_1.no_sync = no_sync.__get__(critic_target_1)
        critic_target_2.no_sync = no_sync.__get__(critic_target_2)

    actors = matd3.actors
    actor_targets = matd3.actor_targets
    actors_pre_learn_sd = [copy.deepcopy(actor.state_dict()) for actor in matd3.actors]
    critics_1 = matd3.critics_1
    critic_targets_1 = matd3.critic_targets_1
    critics_1_pre_learn_sd = [
        str(copy.deepcopy(critic_1.state_dict())) for critic_1 in matd3.critics_1
    ]
    critics_2 = matd3.critics_2
    critic_targets_2 = matd3.critic_targets_2
    critics_2_pre_learn_sd = [
        str(copy.deepcopy(critic_2.state_dict())) for critic_2 in matd3.critics_2
    ]

    for _ in range(4):
        matd3.scores.append(0)
        loss = matd3.learn(accelerated_experiences)

    assert isinstance(loss, dict)
    for agent_id in matd3.agent_ids:
        assert loss[agent_id][-1] >= 0.0
    for old_actor, updated_actor in zip(actors, matd3.actors):
        assert old_actor == updated_actor
    for old_actor_target, updated_actor_target in zip(
        actor_targets, matd3.actor_targets
    ):
        assert old_actor_target == updated_actor_target
    for old_actor_state_dict, updated_actor in zip(actors_pre_learn_sd, matd3.actors):
        assert old_actor_state_dict != str(updated_actor.state_dict())

    for old_critic_1, updated_critic_1 in zip(critics_1, matd3.critics_1):
        assert old_critic_1 == updated_critic_1
    for old_critic_target_1, updated_critic_target_1 in zip(
        critic_targets_1, matd3.critic_targets_1
    ):
        assert old_critic_target_1 == updated_critic_target_1
    for old_critic_1_state_dict, updated_critic_1 in zip(
        critics_1_pre_learn_sd, matd3.critics_1
    ):
        assert old_critic_1_state_dict != str(updated_critic_1.state_dict())

    for old_critic_2, updated_critic_2 in zip(critics_2, matd3.critics_2):
        assert old_critic_2 == updated_critic_2
    for old_critic_target_2, updated_critic_target_2 in zip(
        critic_targets_2, matd3.critic_targets_2
    ):
        assert old_critic_target_2 == updated_critic_target_2
    for old_critic_2_state_dict, updated_critic_2 in zip(
        critics_2_pre_learn_sd, matd3.critics_2
    ):
        assert old_critic_2_state_dict != str(updated_critic_2.state_dict())


def test_matd3_soft_update(device):
    state_dims = [(6,), (6,)]
    action_dims = [2, 2]
    accelerator = None

    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=False,
        n_agents=2,
        agent_ids=["agent_0", "agent_1"],
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=False,
        accelerator=accelerator,
        device=device,
    )

    for (
        actor,
        actor_target,
        critic_1,
        critic_target_1,
        critic_2,
        critic_target_2,
    ) in zip(
        matd3.actors,
        matd3.actor_targets,
        matd3.critics_1,
        matd3.critic_targets_1,
        matd3.critics_2,
        matd3.critic_targets_2,
    ):
        # Check actors
        matd3.soft_update(actor, actor_target)
        eval_params = list(actor.parameters())
        target_params = list(actor_target.parameters())
        expected_params = [
            matd3.tau * eval_param + (1.0 - matd3.tau) * target_param
            for eval_param, target_param in zip(eval_params, target_params)
        ]
        assert all(
            torch.allclose(expected_param, target_param)
            for expected_param, target_param in zip(expected_params, target_params)
        )
        matd3.soft_update(critic_1, critic_target_1)
        eval_params = list(critic_1.parameters())
        target_params = list(critic_target_1.parameters())
        expected_params = [
            matd3.tau * eval_param + (1.0 - matd3.tau) * target_param
            for eval_param, target_param in zip(eval_params, target_params)
        ]

        assert all(
            torch.allclose(expected_param, target_param)
            for expected_param, target_param in zip(expected_params, target_params)
        )
        matd3.soft_update(critic_2, critic_target_2)
        eval_params = list(critic_2.parameters())
        target_params = list(critic_target_2.parameters())
        expected_params = [
            matd3.tau * eval_param + (1.0 - matd3.tau) * target_param
            for eval_param, target_param in zip(eval_params, target_params)
        ]

        assert all(
            torch.allclose(expected_param, target_param)
            for expected_param, target_param in zip(expected_params, target_params)
        )


@pytest.mark.parametrize("compile", [True, False])
def test_matd3_algorithm_test_loop(device, compile):
    state_dims = [(6,), (6,)]
    action_dims = [2, 2]
    accelerator = None

    env = DummyMultiEnv(state_dims[0], action_dims)

    # env = make_vect_envs("CartPole-v1", num_envs=num_envs)
    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=False,
        n_agents=2,
        agent_ids=["agent_0", "agent_1"],
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=True,
        accelerator=accelerator,
        device=device,
        torch_compiler="default" if compile else None,
    )
    mean_score = matd3.test(env, max_steps=10)
    assert isinstance(mean_score, float)


@pytest.mark.parametrize("compile", [True, False])
def test_matd3_algorithm_test_loop_cnn(device, compile):
    env_state_dims = [(32, 32, 3), (32, 32, 3)]
    agent_state_dims = [(3, 32, 32), (3, 32, 32)]
    net_config = {
        "arch": "cnn",
        "hidden_size": [8],
        "channel_size": [16],
        "kernel_size": [3],
        "stride_size": [1],
        "normalize": False,
    }
    action_dims = [2, 2]
    accelerator = None
    env = DummyMultiEnv(env_state_dims[0], action_dims)
    matd3 = MATD3(
        agent_state_dims,
        action_dims,
        one_hot=False,
        n_agents=2,
        agent_ids=["agent_0", "agent_1"],
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        net_config=net_config,
        discrete_actions=False,
        accelerator=accelerator,
        device=device,
        torch_compiler="default" if compile else None,
    )
    mean_score = matd3.test(env, max_steps=10, swap_channels=True)
    assert isinstance(mean_score, float)


@pytest.mark.parametrize("compile", [True, False])
def test_matd3_algorithm_test_loop_cnn_vectorized(device, compile):
    env_state_dims = [(32, 32, 3), (32, 32, 3)]
    agent_state_dims = [(3, 32, 32), (3, 32, 32)]
    net_config = {
        "arch": "cnn",
        "hidden_size": [8],
        "channel_size": [16],
        "kernel_size": [3],
        "stride_size": [1],
        "normalize": False,
    }
    action_dims = [2, 2]
    accelerator = None
    env = make_multi_agent_vect_envs(DummyMultiEnv(env_state_dims[0], action_dims), 2)
    matd3 = MATD3(
        agent_state_dims,
        action_dims,
        one_hot=False,
        n_agents=2,
        agent_ids=["agent_0", "agent_1"],
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        net_config=net_config,
        discrete_actions=False,
        accelerator=accelerator,
        device=device,
        torch_compiler="default" if compile else None,
    )
    mean_score = matd3.test(env, max_steps=10, swap_channels=True)
    assert isinstance(mean_score, float)


@pytest.mark.parametrize(
    "accelerator_flag, wrap, compile",
    [
        (False, True, False),
        (True, True, False),
        (True, False, False),
        (False, True, True),
        (True, True, True),
        (True, False, True),
    ],
)
def test_matd3_clone_returns_identical_agent(accelerator_flag, wrap, compile):
    # Clones the agent and returns an identical copy.
    state_dims = [(4,), (4,)]
    action_dims = [2, 2]
    one_hot = False
    n_agents = 2
    agent_ids = ["agent_0", "agent_1"]
    max_action = [(1,), (1,)]
    min_action = [(-1,), (-1,)]
    expl_noise = 0.1
    discrete_actions = False
    index = 0
    net_config = {"arch": "mlp", "hidden_size": [64, 64]}
    batch_size = 64
    lr_actor = 0.001
    lr_critic = 0.01
    learn_step = 5
    gamma = 0.95
    tau = 0.01
    mut = None
    actor_networks = None
    critic_networks = None
    policy_freq = 2
    device = "cpu"
    if accelerator_flag:
        accelerator = Accelerator(device_placement=False)
    else:
        accelerator = None

    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot,
        n_agents,
        agent_ids,
        max_action,
        min_action,
        discrete_actions,
        expl_noise=expl_noise,
        index=index,
        policy_freq=policy_freq,
        net_config=net_config,
        batch_size=batch_size,
        lr_actor=lr_actor,
        lr_critic=lr_critic,
        learn_step=learn_step,
        gamma=gamma,
        tau=tau,
        mut=mut,
        actor_networks=actor_networks,
        critic_networks=critic_networks,
        device=device,
        accelerator=accelerator,
        torch_compiler="default" if compile else None,
        wrap=wrap,
    )

    clone_agent = matd3.clone(wrap=wrap)

    assert isinstance(clone_agent, MATD3)
    assert clone_agent.state_dims == matd3.state_dims
    assert clone_agent.action_dims == matd3.action_dims
    assert clone_agent.one_hot == matd3.one_hot
    assert clone_agent.n_agents == matd3.n_agents
    assert clone_agent.agent_ids == matd3.agent_ids
    assert clone_agent.max_action == matd3.max_action
    assert clone_agent.min_action == matd3.min_action
    assert np.array_equal(clone_agent.expl_noise, matd3.expl_noise)
    assert clone_agent.discrete_actions == matd3.discrete_actions
    assert clone_agent.index == matd3.index
    assert clone_agent.net_config == matd3.net_config
    assert clone_agent.batch_size == matd3.batch_size
    assert clone_agent.lr_actor == matd3.lr_actor
    assert clone_agent.lr_critic == matd3.lr_critic
    assert clone_agent.learn_step == matd3.learn_step
    assert clone_agent.gamma == matd3.gamma
    assert clone_agent.tau == matd3.tau
    assert clone_agent.device == matd3.device
    assert clone_agent.accelerator == matd3.accelerator

    assert clone_agent.torch_compiler == matd3.torch_compiler
    assert clone_agent.CUDA_CACHE_POLICY == matd3.CUDA_CACHE_POLICY

    for clone_actor, actor in zip(clone_agent.actors, matd3.actors):
        assert str(clone_actor.state_dict()) == str(actor.state_dict())
    for clone_critic_1, critic_1 in zip(clone_agent.critics_1, matd3.critics_1):
        assert str(clone_critic_1.state_dict()) == str(critic_1.state_dict())
    for clone_actor_target, actor_target in zip(
        clone_agent.actor_targets, matd3.actor_targets
    ):
        assert str(clone_actor_target.state_dict()) == str(actor_target.state_dict())
    for clone_critic_target_1, critic_target_1 in zip(
        clone_agent.critic_targets_1, matd3.critic_targets_1
    ):
        assert str(clone_critic_target_1.state_dict()) == str(
            critic_target_1.state_dict()
        )

    for clone_critic_2, critic_2 in zip(clone_agent.critics_2, matd3.critics_2):
        assert str(clone_critic_2.state_dict()) == str(critic_2.state_dict())

    for clone_critic_target_2, critic_target_2 in zip(
        clone_agent.critic_targets_2, matd3.critic_targets_2
    ):
        assert str(clone_critic_target_2.state_dict()) == str(
            critic_target_2.state_dict()
        )

    assert clone_agent.actor_networks == matd3.actor_networks
    assert clone_agent.critic_networks == matd3.critic_networks


def test_clone_new_index():
    state_dims = [(4,), (4,)]
    action_dims = [2, 2]
    one_hot = False
    n_agents = 2
    agent_ids = ["agent_0", "agent_1"]
    max_action = [(1,), (1,)]
    min_action = [(-1,), (-1,)]
    discrete_actions = False

    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot,
        n_agents,
        agent_ids,
        max_action,
        min_action,
        discrete_actions,
    )
    clone_agent = matd3.clone(index=100)

    assert clone_agent.index == 100


@pytest.mark.parametrize("compile", [True, False])
def test_clone_after_learning(compile):
    state_dims = [(4,), (4,)]
    action_dims = [2, 2]
    one_hot = False
    n_agents = 2
    agent_ids = ["agent_0", "agent_1"]
    max_action = [(1,), (1,)]
    min_action = [(-1,), (-1,)]
    discrete_actions = False
    batch_size = 8

    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot,
        n_agents,
        agent_ids,
        max_action,
        min_action,
        discrete_actions,
        batch_size=batch_size,
        torch_compiler="default" if compile else None,
    )

    states = {
        agent_id: torch.randn(batch_size, state_dims[idx][0])
        for idx, agent_id in enumerate(agent_ids)
    }
    actions = {
        agent_id: torch.randn(batch_size, action_dims[idx])
        for idx, agent_id in enumerate(agent_ids)
    }
    rewards = {agent_id: torch.randn(batch_size, 1) for agent_id in agent_ids}
    next_states = {
        agent_id: torch.randn(batch_size, state_dims[idx][0])
        for idx, agent_id in enumerate(agent_ids)
    }
    dones = {agent_id: torch.zeros(batch_size, 1) for agent_id in agent_ids}

    experiences = states, actions, rewards, next_states, dones
    matd3.learn(experiences)
    clone_agent = matd3.clone()
    assert isinstance(clone_agent, MATD3)
    assert clone_agent.state_dims == matd3.state_dims
    assert clone_agent.action_dims == matd3.action_dims
    assert clone_agent.one_hot == matd3.one_hot
    assert clone_agent.n_agents == matd3.n_agents
    assert clone_agent.agent_ids == matd3.agent_ids
    assert clone_agent.max_action == matd3.max_action
    assert clone_agent.min_action == matd3.min_action
    assert np.array_equal(clone_agent.expl_noise, matd3.expl_noise)
    assert clone_agent.discrete_actions == matd3.discrete_actions
    assert clone_agent.index == matd3.index
    assert clone_agent.net_config == matd3.net_config
    assert clone_agent.batch_size == matd3.batch_size
    assert clone_agent.lr_actor == matd3.lr_actor
    assert clone_agent.lr_critic == matd3.lr_critic
    assert clone_agent.learn_step == matd3.learn_step
    assert clone_agent.gamma == matd3.gamma
    assert clone_agent.tau == matd3.tau
    assert clone_agent.device == matd3.device
    assert clone_agent.accelerator == matd3.accelerator

    assert clone_agent.torch_compiler == matd3.torch_compiler
    assert clone_agent.CUDA_CACHE_POLICY == matd3.CUDA_CACHE_POLICY

    for clone_actor, actor in zip(clone_agent.actors, matd3.actors):
        assert str(clone_actor.state_dict()) == str(actor.state_dict())
    for clone_critic_1, critic_1 in zip(clone_agent.critics_1, matd3.critics_1):
        assert str(clone_critic_1.state_dict()) == str(critic_1.state_dict())
    for clone_actor_target, actor_target in zip(
        clone_agent.actor_targets, matd3.actor_targets
    ):
        assert str(clone_actor_target.state_dict()) == str(actor_target.state_dict())
    for clone_critic_target_1, critic_target_1 in zip(
        clone_agent.critic_targets_1, matd3.critic_targets_1
    ):
        assert str(clone_critic_target_1.state_dict()) == str(
            critic_target_1.state_dict()
        )

    for clone_critic_2, critic_2 in zip(clone_agent.critics_2, matd3.critics_2):
        assert str(clone_critic_2.state_dict()) == str(critic_2.state_dict())

    for clone_critic_target_2, critic_target_2 in zip(
        clone_agent.critic_targets_2, matd3.critic_targets_2
    ):
        assert str(clone_critic_target_2.state_dict()) == str(
            critic_target_2.state_dict()
        )

    for clone_actor_opt, actor_opt in zip(
        clone_agent.actor_optimizers, matd3.actor_optimizers
    ):
        assert str(clone_actor_opt) == str(actor_opt)
    for clone_critic_opt_1, critic_opt_1 in zip(
        clone_agent.critic_1_optimizers, matd3.critic_1_optimizers
    ):
        assert str(clone_critic_opt_1) == str(critic_opt_1)
    for clone_critic_opt_2, critic_opt_2 in zip(
        clone_agent.critic_2_optimizers, matd3.critic_2_optimizers
    ):
        assert str(clone_critic_opt_2) == str(critic_opt_2)

    assert clone_agent.actor_networks == matd3.actor_networks
    assert clone_agent.critic_networks == matd3.critic_networks


@pytest.mark.parametrize("checkpoint_compile", [True, False])
@pytest.mark.parametrize("loaded_compile", [True, False])
def test_matd3_save_load_checkpoint_correct_data_and_format(
    tmpdir, checkpoint_compile, loaded_compile
):
    net_config = {"arch": "mlp", "hidden_size": [32, 32]}
    # Initialize the ddpg agent
    matd3 = MATD3(
        state_dims=[
            [
                6,
            ]
        ],
        action_dims=[2],
        one_hot=False,
        n_agents=1,
        agent_ids=["agent_0"],
        max_action=[[1]],
        min_action=[[-1]],
        net_config=net_config,
        discrete_actions=True,
        torch_compiler="default" if checkpoint_compile else None,
    )

    # Save the checkpoint to a file
    checkpoint_path = Path(tmpdir) / "checkpoint.pth"
    matd3.save_checkpoint(checkpoint_path)

    # Load the saved checkpoint file
    checkpoint = torch.load(checkpoint_path, pickle_module=dill)

    # Check if the loaded checkpoint has the correct keys
    assert "actors_init_dict" in checkpoint
    assert "actors_state_dict" in checkpoint
    assert "actor_targets_init_dict" in checkpoint
    assert "actor_targets_state_dict" in checkpoint
    assert "actor_optimizers_state_dict" in checkpoint
    assert "critics_1_init_dict" in checkpoint
    assert "critics_1_state_dict" in checkpoint
    assert "critic_targets_1_init_dict" in checkpoint
    assert "critic_targets_1_state_dict" in checkpoint
    assert "critic_2_optimizers_state_dict" in checkpoint
    assert "critics_2_init_dict" in checkpoint
    assert "critics_2_state_dict" in checkpoint
    assert "critic_targets_2_init_dict" in checkpoint
    assert "critic_targets_2_state_dict" in checkpoint
    assert "critic_2_optimizers_state_dict" in checkpoint
    assert "policy_freq" in checkpoint
    assert "net_config" in checkpoint
    assert "batch_size" in checkpoint
    assert "lr_actor" in checkpoint
    assert "lr_critic" in checkpoint
    assert "learn_step" in checkpoint
    assert "gamma" in checkpoint
    assert "tau" in checkpoint
    assert "mut" in checkpoint
    assert "index" in checkpoint
    assert "scores" in checkpoint
    assert "fitness" in checkpoint
    assert "steps" in checkpoint

    # Load checkpoint
    loaded_matd3 = MATD3(
        state_dims=[
            [
                6,
            ]
        ],
        action_dims=[2],
        one_hot=False,
        n_agents=1,
        agent_ids=["agent_0"],
        max_action=[(1,)],
        min_action=[(-1,)],
        discrete_actions=True,
        torch_compiler="default" if loaded_compile else None,
    )
    loaded_matd3.load_checkpoint(checkpoint_path)
    # Check if properties and weights are loaded correctly
    assert loaded_matd3.net_config == net_config
    # checkpoint compile will overwrite the loaded compile
    if not loaded_compile:
        assert all(isinstance(actor, EvolvableMLP) for actor in loaded_matd3.actors)
        assert all(
            isinstance(actor_target, EvolvableMLP)
            for actor_target in loaded_matd3.actor_targets
        )
        assert all(
            isinstance(critic_1, EvolvableMLP) for critic_1 in loaded_matd3.critics_1
        )
        assert all(
            isinstance(critic_target_1, EvolvableMLP)
            for critic_target_1 in loaded_matd3.critic_targets_1
        )
        assert all(
            isinstance(critic_2, EvolvableMLP) for critic_2 in loaded_matd3.critics_2
        )
        assert all(
            isinstance(critic_target_2, EvolvableMLP)
            for critic_target_2 in loaded_matd3.critic_targets_2
        )
    else:
        # TODO check EvolvableMLP in detail (class attributes and values except for the wrapped networks)
        #   because this isn't state_dict
        assert all(
            isinstance(actor, torch._dynamo.eval_frame.OptimizedModule)
            for actor in loaded_matd3.actors
        )
        assert all(
            isinstance(actor_target, torch._dynamo.eval_frame.OptimizedModule)
            for actor_target in loaded_matd3.actor_targets
        )
        assert all(
            isinstance(critic_1, torch._dynamo.eval_frame.OptimizedModule)
            for critic_1 in loaded_matd3.critics_1
        )
        assert all(
            isinstance(critic_target_1, torch._dynamo.eval_frame.OptimizedModule)
            for critic_target_1 in loaded_matd3.critic_targets_1
        )
        assert all(
            isinstance(critic_2, torch._dynamo.eval_frame.OptimizedModule)
            for critic_2 in loaded_matd3.critics_2
        )
        assert all(
            isinstance(critic_target_2, torch._dynamo.eval_frame.OptimizedModule)
            for critic_target_2 in loaded_matd3.critic_targets_2
        )
    assert matd3.lr_actor == 0.001
    assert matd3.lr_critic == 0.01

    for actor, actor_target in zip(loaded_matd3.actors, loaded_matd3.actor_targets):
        assert str(actor.state_dict()) == str(actor_target.state_dict())

    for critic_1, critic_target_1 in zip(
        loaded_matd3.critics_1, loaded_matd3.critic_targets_1
    ):
        assert str(critic_1.state_dict()) == str(critic_target_1.state_dict())

    for critic_2, critic_target_2 in zip(
        loaded_matd3.critics_2, loaded_matd3.critic_targets_2
    ):
        assert str(critic_2.state_dict()) == str(critic_target_2.state_dict())

    assert matd3.batch_size == 64
    assert matd3.learn_step == 5
    assert matd3.gamma == 0.95
    assert matd3.tau == 0.01
    assert matd3.mut is None
    assert matd3.index == 0
    assert matd3.scores == []
    assert matd3.fitness == []
    assert matd3.steps == [0]
    assert matd3.policy_freq == 2


@pytest.mark.parametrize("checkpoint_compile", [True, False])
@pytest.mark.parametrize("loaded_compile", [True, False])
def test_matd3_save_load_checkpoint_correct_data_and_format_cnn(
    tmpdir, checkpoint_compile, loaded_compile
):
    net_config_cnn = {
        "arch": "cnn",
        "hidden_size": [8],
        "channel_size": [16],
        "kernel_size": [3],
        "stride_size": [1],
        "normalize": False,
    }
    policy_freq = 2
    # Initialize the ddpg agent
    matd3 = MATD3(
        state_dims=[[3, 32, 32]],
        action_dims=[2],
        one_hot=False,
        n_agents=1,
        agent_ids=["agent_0"],
        net_config=net_config_cnn,
        max_action=[[1]],
        min_action=[[-1]],
        discrete_actions=True,
        policy_freq=policy_freq,
        torch_compiler="default" if checkpoint_compile else None,
    )

    # Save the checkpoint to a file
    checkpoint_path = Path(tmpdir) / "checkpoint.pth"
    matd3.save_checkpoint(checkpoint_path)

    # Load the saved checkpoint file
    checkpoint = torch.load(checkpoint_path, pickle_module=dill)

    # Check if the loaded checkpoint has the correct keys
    assert "actors_init_dict" in checkpoint
    assert "actors_state_dict" in checkpoint
    assert "actor_targets_init_dict" in checkpoint
    assert "actor_targets_state_dict" in checkpoint
    assert "actor_optimizers_state_dict" in checkpoint
    assert "critics_1_init_dict" in checkpoint
    assert "critics_1_state_dict" in checkpoint
    assert "critic_targets_1_init_dict" in checkpoint
    assert "critic_targets_1_state_dict" in checkpoint
    assert "critic_1_optimizers_state_dict" in checkpoint
    assert "critics_2_init_dict" in checkpoint
    assert "critics_2_state_dict" in checkpoint
    assert "critic_targets_2_init_dict" in checkpoint
    assert "critic_targets_2_state_dict" in checkpoint
    assert "critic_2_optimizers_state_dict" in checkpoint
    assert "net_config" in checkpoint
    assert "batch_size" in checkpoint
    assert "lr_actor" in checkpoint
    assert "lr_critic" in checkpoint
    assert "learn_step" in checkpoint
    assert "gamma" in checkpoint
    assert "tau" in checkpoint
    assert "mut" in checkpoint
    assert "index" in checkpoint
    assert "scores" in checkpoint
    assert "fitness" in checkpoint
    assert "steps" in checkpoint
    assert "policy_freq" in checkpoint

    # Load checkpoint
    loaded_matd3 = MATD3(
        state_dims=[[3, 32, 32]],
        action_dims=[2],
        one_hot=False,
        n_agents=1,
        agent_ids=["agent_0"],
        max_action=[(1,)],
        min_action=[(-1,)],
        discrete_actions=True,
        torch_compiler="default" if loaded_compile else None,
    )
    loaded_matd3.load_checkpoint(checkpoint_path)

    # Check if properties and weights are loaded correctly
    assert loaded_matd3.net_config == net_config_cnn
    # checkpoint compile will overwrite the loaded compile
    if not loaded_compile:
        assert all(isinstance(actor, EvolvableCNN) for actor in loaded_matd3.actors)
        assert all(
            isinstance(actor_target, EvolvableCNN)
            for actor_target in loaded_matd3.actor_targets
        )
        assert all(
            isinstance(critic_1, EvolvableCNN) for critic_1 in loaded_matd3.critics_1
        )
        assert all(
            isinstance(critic_target_1, EvolvableCNN)
            for critic_target_1 in loaded_matd3.critic_targets_1
        )
        assert all(
            isinstance(critic_2, EvolvableCNN) for critic_2 in loaded_matd3.critics_2
        )
        assert all(
            isinstance(critic_target_2, EvolvableCNN)
            for critic_target_2 in loaded_matd3.critic_targets_2
        )
    else:
        # TODO check EvolvableMLP in detail (class attributes and values except for the wrapped networks)
        #   because this isn't state_dict
        assert all(
            isinstance(actor, torch._dynamo.eval_frame.OptimizedModule)
            for actor in loaded_matd3.actors
        )
        assert all(
            isinstance(actor_target, torch._dynamo.eval_frame.OptimizedModule)
            for actor_target in loaded_matd3.actor_targets
        )
        assert all(
            isinstance(critic_1, torch._dynamo.eval_frame.OptimizedModule)
            for critic_1 in loaded_matd3.critics_1
        )
        assert all(
            isinstance(critic_target_1, torch._dynamo.eval_frame.OptimizedModule)
            for critic_target_1 in loaded_matd3.critic_targets_1
        )
        assert all(
            isinstance(critic_2, torch._dynamo.eval_frame.OptimizedModule)
            for critic_2 in loaded_matd3.critics_2
        )
        assert all(
            isinstance(critic_target_2, torch._dynamo.eval_frame.OptimizedModule)
            for critic_target_2 in loaded_matd3.critic_targets_2
        )
    assert matd3.lr_actor == 0.001
    assert matd3.lr_critic == 0.01

    for actor, actor_target in zip(loaded_matd3.actors, loaded_matd3.actor_targets):
        assert str(actor.state_dict()) == str(actor_target.state_dict())

    for critic_1, critic_target_1 in zip(
        loaded_matd3.critics_1, loaded_matd3.critic_targets_1
    ):
        assert str(critic_1.state_dict()) == str(critic_target_1.state_dict())

    for critic_2, critic_target_2 in zip(
        loaded_matd3.critics_2, loaded_matd3.critic_targets_2
    ):
        assert str(critic_2.state_dict()) == str(critic_target_2.state_dict())

    assert matd3.batch_size == 64
    assert matd3.learn_step == 5
    assert matd3.gamma == 0.95
    assert matd3.tau == 0.01
    assert matd3.mut is None
    assert matd3.index == 0
    assert matd3.scores == []
    assert matd3.fitness == []
    assert matd3.steps == [0]
    assert matd3.policy_freq == policy_freq


@pytest.mark.parametrize(
    "state_dims, action_dims",
    [
        (
            [
                [
                    6,
                ]
            ],
            [2],
        ),
        (
            [
                [
                    6,
                ]
            ],
            [2],
        ),
    ],
)
@pytest.mark.parametrize("checkpoint_compile", [True, False])
@pytest.mark.parametrize("loaded_compile", [True, False])
def test_matd3_save_load_checkpoint_correct_data_and_format_make_evo(
    tmpdir,
    state_dims,
    action_dims,
    mlp_actor,
    mlp_critic,
    checkpoint_compile,
    loaded_compile,
    device,
):
    evo_actors = [
        MakeEvolvable(network=mlp_actor, input_tensor=torch.randn(1, 6), device=device)
        for _ in range(1)
    ]
    evo_critics_1 = [
        MakeEvolvable(network=mlp_critic, input_tensor=torch.randn(1, 8), device=device)
        for _ in range(1)
    ]
    evo_critics_2 = [
        MakeEvolvable(network=mlp_critic, input_tensor=torch.randn(1, 8), device=device)
        for _ in range(1)
    ]
    evo_critics = [evo_critics_1, evo_critics_2]
    matd3 = MATD3(
        state_dims=state_dims,
        action_dims=action_dims,
        one_hot=False,
        n_agents=1,
        agent_ids=["agent_0"],
        max_action=[[1]],
        min_action=[[-1]],
        discrete_actions=True,
        actor_networks=evo_actors,
        critic_networks=evo_critics,
        torch_compiler="default" if checkpoint_compile else None,
        device=device,
    )
    # Save the checkpoint to a file
    checkpoint_path = Path(tmpdir) / "checkpoint.pth"
    matd3.save_checkpoint(checkpoint_path)

    # Load the saved checkpoint file
    checkpoint = torch.load(checkpoint_path, pickle_module=dill)

    # Check if the loaded checkpoint has the correct keys
    assert "actors_init_dict" in checkpoint
    assert "actors_state_dict" in checkpoint
    assert "actor_targets_init_dict" in checkpoint
    assert "actor_targets_state_dict" in checkpoint
    assert "actor_optimizers_state_dict" in checkpoint
    assert "critics_1_init_dict" in checkpoint
    assert "critics_1_state_dict" in checkpoint
    assert "critic_targets_1_init_dict" in checkpoint
    assert "critic_targets_1_state_dict" in checkpoint
    assert "critic_1_optimizers_state_dict" in checkpoint
    assert "critics_2_init_dict" in checkpoint
    assert "critics_2_state_dict" in checkpoint
    assert "critic_targets_2_init_dict" in checkpoint
    assert "critic_targets_2_state_dict" in checkpoint
    assert "critic_2_optimizers_state_dict" in checkpoint
    assert "net_config" in checkpoint
    assert "batch_size" in checkpoint
    assert "lr_actor" in checkpoint
    assert "lr_critic" in checkpoint
    assert "learn_step" in checkpoint
    assert "gamma" in checkpoint
    assert "tau" in checkpoint
    assert "mut" in checkpoint
    assert "index" in checkpoint
    assert "scores" in checkpoint
    assert "fitness" in checkpoint
    assert "steps" in checkpoint
    assert "policy_freq" in checkpoint

    # Load checkpoint
    loaded_matd3 = MATD3(
        state_dims=[[3, 32, 32]],
        action_dims=[2],
        one_hot=False,
        n_agents=1,
        agent_ids=["agent_0"],
        max_action=[(1,)],
        min_action=[(-1,)],
        discrete_actions=True,
        torch_compiler="default" if loaded_compile else None,
    )
    loaded_matd3.load_checkpoint(checkpoint_path)

    # Check if properties and weights are loaded correctly
    # checkpoint compile will overwrite the loaded compile

    if not loaded_compile:
        assert all(isinstance(actor, MakeEvolvable) for actor in loaded_matd3.actors)
        assert all(
            isinstance(actor_target, MakeEvolvable)
            for actor_target in loaded_matd3.actor_targets
        )
        assert all(
            isinstance(critic_1, MakeEvolvable) for critic_1 in loaded_matd3.critics_1
        )
        assert all(
            isinstance(critic_target_1, MakeEvolvable)
            for critic_target_1 in loaded_matd3.critic_targets_1
        )
        assert all(
            isinstance(critic_2, MakeEvolvable) for critic_2 in loaded_matd3.critics_2
        )
        assert all(
            isinstance(critic_target_2, MakeEvolvable)
            for critic_target_2 in loaded_matd3.critic_targets_2
        )
    else:
        # TODO check EvolvableMLP in detail (class attributes and values except for the wrapped networks)
        #   because this isn't state_dict
        assert all(
            isinstance(actor, torch._dynamo.eval_frame.OptimizedModule)
            for actor in loaded_matd3.actors
        )
        assert all(
            isinstance(actor_target, torch._dynamo.eval_frame.OptimizedModule)
            for actor_target in loaded_matd3.actor_targets
        )
        assert all(
            isinstance(critic_1, torch._dynamo.eval_frame.OptimizedModule)
            for critic_1 in loaded_matd3.critics_1
        )
        assert all(
            isinstance(critic_target_1, torch._dynamo.eval_frame.OptimizedModule)
            for critic_target_1 in loaded_matd3.critic_targets_1
        )
        assert all(
            isinstance(critic_2, torch._dynamo.eval_frame.OptimizedModule)
            for critic_2 in loaded_matd3.critics_2
        )
        assert all(
            isinstance(critic_target_2, torch._dynamo.eval_frame.OptimizedModule)
            for critic_target_2 in loaded_matd3.critic_targets_2
        )
    assert matd3.lr_actor == 0.001
    assert matd3.lr_critic == 0.01

    for actor, actor_target in zip(loaded_matd3.actors, loaded_matd3.actor_targets):
        assert str(actor.state_dict()) == str(actor_target.state_dict())

    for critic_1, critic_target_1 in zip(
        loaded_matd3.critics_1, loaded_matd3.critic_targets_1
    ):
        assert str(critic_1.state_dict()) == str(critic_target_1.state_dict())

    for critic_2, critic_target_2 in zip(
        loaded_matd3.critics_2, loaded_matd3.critic_targets_2
    ):
        assert str(critic_2.state_dict()) == str(critic_target_2.state_dict())

    assert matd3.batch_size == 64
    assert matd3.learn_step == 5
    assert matd3.gamma == 0.95
    assert matd3.tau == 0.01
    assert matd3.mut is None
    assert matd3.index == 0
    assert matd3.scores == []
    assert matd3.fitness == []
    assert matd3.steps == [0]
    assert matd3.policy_freq == 2


@pytest.mark.parametrize("compile", [True, False])
def test_matd3_unwrap_models(compile):
    state_dims = [(6,), (6,)]
    action_dims = [2, 2]
    accelerator = Accelerator()
    matd3 = MATD3(
        state_dims,
        action_dims,
        one_hot=False,
        n_agents=2,
        agent_ids=["agent_0", "agent_1"],
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=True,
        accelerator=accelerator,
        torch_compiler="default" if compile else None,
    )
    matd3.unwrap_models()
    for (
        actor,
        critic_1,
        critic_2,
        actor_target,
        critic_target_1,
        critic_target_2,
    ) in zip(
        matd3.actors,
        matd3.critics_1,
        matd3.critics_2,
        matd3.actor_targets,
        matd3.critic_targets_1,
        matd3.critic_targets_2,
    ):
        assert isinstance(actor, nn.Module)
        assert isinstance(actor_target, nn.Module)
        assert isinstance(critic_1, nn.Module)
        assert isinstance(critic_target_1, nn.Module)
        assert isinstance(critic_2, nn.Module)
        assert isinstance(critic_target_2, nn.Module)


# Returns the input action scaled to the action space defined by self.min_action and self.max_action.
@pytest.mark.parametrize("compile", [True, False])
def test_action_scaling(compile):
    action = torch.Tensor([0.1, 0.2, 0.3, -0.1, -0.2, -0.3])
    max_actions = [(1,), (2,), (1,), (2,), (2,)]
    min_actions = [(-1,), (-2,), (0,), (0,), (-1,)]

    matd3 = MATD3(
        state_dims=[[4], [4], [4], [4], [4]],
        action_dims=[1, 1, 1, 1, 1],
        n_agents=5,
        agent_ids=["agent_0", "agent_1", "agent_2", "agent_3", "agent_4"],
        discrete_actions=False,
        one_hot=False,
        max_action=max_actions,
        min_action=min_actions,
        torch_compiler="default" if compile else None,
    )
    matd3.actors[0].mlp_output_activation = "Tanh"
    scaled_action = matd3.scale_to_action_space(action, idx=0)
    assert torch.equal(scaled_action, torch.Tensor([0.1, 0.2, 0.3, -0.1, -0.2, -0.3]))

    matd3.actors[1].mlp_output_activation = "Tanh"
    action = torch.Tensor([0.1, 0.2, 0.3, -0.1, -0.2, -0.3])
    scaled_action = matd3.scale_to_action_space(action, idx=1)
    torch.testing.assert_close(
        scaled_action, torch.Tensor([0.2, 0.4, 0.6, -0.2, -0.4, -0.6])
    )

    matd3.actors[2].mlp_output_activation = "Sigmoid"
    action = torch.Tensor([0.1, 0.2, 0.3, 0])
    scaled_action = matd3.scale_to_action_space(action, idx=2)
    assert torch.equal(scaled_action, torch.Tensor([0.1, 0.2, 0.3, 0]))

    matd3.actors[3].mlp_output_activation = "GumbelSoftmax"
    action = torch.Tensor([0.1, 0.2, 0.3, 0])
    scaled_action = matd3.scale_to_action_space(action, idx=3)
    assert torch.equal(scaled_action, torch.Tensor([0.2, 0.4, 0.6, 0]))

    matd3.actors[4].mlp_output_activation = "Tanh"
    action = torch.Tensor([0.1, 0.2, 0.3, -0.1, -0.2, -0.3])
    scaled_action = matd3.scale_to_action_space(action, idx=4)
    torch.testing.assert_close(
        scaled_action,
        torch.Tensor(
            [
                0.55 * 3 - 1,
                0.6 * 3 - 1,
                0.65 * 3 - 1,
                0.45 * 3 - 1,
                0.4 * 3 - 1,
                0.35 * 3 - 1,
            ]
        ),
    )


@pytest.mark.parametrize(
    "device, accelerator, compile",
    [
        ("cpu", None, False),
        ("cpu", Accelerator(), False),
        ("cpu", None, True),
        ("cpu", Accelerator(), True),
    ],
)
# The saved checkpoint file contains the correct data and format.
def test_load_from_pretrained(device, accelerator, compile, tmpdir):
    # Initialize the matd3 agent
    matd3 = MATD3(
        state_dims=[[4], [4]],
        action_dims=[2, 2],
        one_hot=False,
        n_agents=2,
        agent_ids=["agent_0", "agent_1"],
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=True,
        torch_compiler="default" if compile else None,
    )

    # Save the checkpoint to a file
    checkpoint_path = Path(tmpdir) / "checkpoint.pth"
    matd3.save_checkpoint(checkpoint_path)

    # Create new agent object
    new_matd3 = MATD3.load(checkpoint_path, device=device, accelerator=accelerator)

    # Check if properties and weights are loaded correctly
    assert new_matd3.state_dims == matd3.state_dims
    assert new_matd3.action_dims == matd3.action_dims
    assert new_matd3.one_hot == matd3.one_hot
    assert new_matd3.n_agents == matd3.n_agents
    assert new_matd3.agent_ids == matd3.agent_ids
    assert new_matd3.min_action == matd3.min_action
    assert new_matd3.max_action == matd3.max_action
    assert new_matd3.net_config == matd3.net_config
    assert new_matd3.lr_actor == matd3.lr_actor
    assert new_matd3.lr_critic == matd3.lr_critic
    for (
        new_actor,
        new_actor_target,
        new_critic_1,
        new_critic_target_1,
        new_critic_2,
        new_critic_target_2,
        actor,
        actor_target,
        critic_1,
        critic_target_1,
        critic_2,
        critic_target_2,
    ) in zip(
        new_matd3.actors,
        new_matd3.actor_targets,
        new_matd3.critics_1,
        new_matd3.critic_targets_1,
        new_matd3.critics_2,
        new_matd3.critic_targets_2,
        matd3.actors,
        matd3.actor_targets,
        matd3.critics_1,
        matd3.critic_targets_1,
        matd3.critics_2,
        matd3.critic_targets_2,
    ):
        if not compile:
            assert isinstance(new_actor, EvolvableMLP)
            assert isinstance(new_actor_target, EvolvableMLP)
            assert isinstance(new_critic_1, EvolvableMLP)
            assert isinstance(new_critic_target_1, EvolvableMLP)
            assert isinstance(new_critic_2, EvolvableMLP)
            assert isinstance(new_critic_target_2, EvolvableMLP)
        else:
            assert isinstance(new_actor, torch._dynamo.eval_frame.OptimizedModule)
            assert isinstance(
                new_actor_target, torch._dynamo.eval_frame.OptimizedModule
            )
            assert isinstance(new_critic_1, torch._dynamo.eval_frame.OptimizedModule)
            assert isinstance(
                new_critic_target_1, torch._dynamo.eval_frame.OptimizedModule
            )
            assert isinstance(new_critic_2, torch._dynamo.eval_frame.OptimizedModule)
            assert isinstance(
                new_critic_target_2, torch._dynamo.eval_frame.OptimizedModule
            )
        assert str(new_actor.to("cpu").state_dict()) == str(actor.state_dict())
        assert str(new_actor_target.to("cpu").state_dict()) == str(
            actor_target.state_dict()
        )
        assert str(new_critic_1.to("cpu").state_dict()) == str(critic_1.state_dict())
        assert str(new_critic_target_1.to("cpu").state_dict()) == str(
            critic_target_1.state_dict()
        )
        assert str(new_critic_2.to("cpu").state_dict()) == str(critic_2.state_dict())
        assert str(new_critic_target_2.to("cpu").state_dict()) == str(
            critic_target_2.state_dict()
        )
    assert new_matd3.batch_size == matd3.batch_size
    assert new_matd3.learn_step == matd3.learn_step
    assert new_matd3.gamma == matd3.gamma
    assert new_matd3.tau == matd3.tau
    assert new_matd3.mut == matd3.mut
    assert new_matd3.index == matd3.index
    assert new_matd3.scores == matd3.scores
    assert new_matd3.fitness == matd3.fitness
    assert new_matd3.steps == matd3.steps


@pytest.mark.parametrize(
    "device, accelerator, compile",
    [
        ("cpu", None, False),
        ("cpu", Accelerator(), False),
        ("cpu", None, True),
        ("cpu", Accelerator(), True),
    ],
)
# The saved checkpoint file contains the correct data and format.
def test_load_from_pretrained_cnn(device, accelerator, compile, tmpdir):
    # Initialize the matd3 agent
    matd3 = MATD3(
        state_dims=[[3, 32, 32], [3, 32, 32]],
        action_dims=[2, 2],
        one_hot=False,
        n_agents=2,
        agent_ids=["agent_a", "agent_b"],
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=False,
        net_config={
            "arch": "cnn",
            "hidden_size": [8],
            "channel_size": [3],
            "kernel_size": [3],
            "stride_size": [1],
            "normalize": False,
        },
    )

    # Save the checkpoint to a file
    checkpoint_path = Path(tmpdir) / "checkpoint.pth"
    matd3.save_checkpoint(checkpoint_path)

    # Create new agent object
    new_matd3 = MATD3.load(checkpoint_path, device=device, accelerator=accelerator)

    # Check if properties and weights are loaded correctly
    assert new_matd3.state_dims == matd3.state_dims
    assert new_matd3.action_dims == matd3.action_dims
    assert new_matd3.one_hot == matd3.one_hot
    assert new_matd3.n_agents == matd3.n_agents
    assert new_matd3.agent_ids == matd3.agent_ids
    assert new_matd3.min_action == matd3.min_action
    assert new_matd3.max_action == matd3.max_action
    assert new_matd3.net_config == matd3.net_config
    assert new_matd3.lr_actor == matd3.lr_actor
    assert new_matd3.lr_critic == matd3.lr_critic
    for (
        new_actor,
        new_actor_target,
        new_critic_1,
        new_critic_target_1,
        new_critic_2,
        new_critic_target_2,
        actor,
        actor_target,
        critic_1,
        critic_target_1,
        critic_2,
        critic_target_2,
    ) in zip(
        new_matd3.actors,
        new_matd3.actor_targets,
        new_matd3.critics_1,
        new_matd3.critic_targets_1,
        new_matd3.critics_2,
        new_matd3.critic_targets_2,
        matd3.actors,
        matd3.actor_targets,
        matd3.critics_1,
        matd3.critic_targets_1,
        matd3.critics_2,
        matd3.critic_targets_2,
    ):
        assert isinstance(new_actor, EvolvableCNN)
        assert isinstance(new_actor_target, EvolvableCNN)
        assert isinstance(new_critic_1, EvolvableCNN)
        assert isinstance(new_critic_target_1, EvolvableCNN)
        assert isinstance(new_critic_2, EvolvableCNN)
        assert isinstance(new_critic_target_2, EvolvableCNN)
        assert str(new_actor.to("cpu").state_dict()) == str(actor.state_dict())
        assert str(new_actor_target.to("cpu").state_dict()) == str(
            actor_target.state_dict()
        )
        assert str(new_critic_1.to("cpu").state_dict()) == str(critic_1.state_dict())
        assert str(new_critic_target_1.to("cpu").state_dict()) == str(
            critic_target_1.state_dict()
        )
        assert str(new_critic_2.to("cpu").state_dict()) == str(critic_2.state_dict())
        assert str(new_critic_target_2.to("cpu").state_dict()) == str(
            critic_target_2.state_dict()
        )
    assert new_matd3.batch_size == matd3.batch_size
    assert new_matd3.learn_step == matd3.learn_step
    assert new_matd3.gamma == matd3.gamma
    assert new_matd3.tau == matd3.tau
    assert new_matd3.mut == matd3.mut
    assert new_matd3.index == matd3.index
    assert new_matd3.scores == matd3.scores
    assert new_matd3.fitness == matd3.fitness
    assert new_matd3.steps == matd3.steps


@pytest.mark.parametrize(
    "state_dims, action_dims, arch, input_tensor, critic_input_tensor, secondary_input_tensor",
    [
        (
            [[4], [4]],
            [2, 2],
            "mlp",
            torch.randn(1, 4),
            torch.randn(1, 6),
            None,
        ),
        (
            [[4, 210, 160], [4, 210, 160]],
            [2, 2],
            "cnn",
            torch.randn(1, 4, 2, 210, 160),
            torch.randn(1, 4, 2, 210, 160),
            torch.randn(1, 2),
        ),
    ],
)
# The saved checkpoint file contains the correct data and format.
def test_load_from_pretrained_networks(
    mlp_actor,
    mlp_critic,
    cnn_actor,
    cnn_critic,
    state_dims,
    action_dims,
    arch,
    input_tensor,
    critic_input_tensor,
    secondary_input_tensor,
    tmpdir,
):
    one_hot = False
    if arch == "mlp":
        actor_network = mlp_actor
        critic_network = mlp_critic
    elif arch == "cnn":
        actor_network = cnn_actor
        critic_network = cnn_critic

    actor_network = MakeEvolvable(actor_network, input_tensor)
    critic_network = MakeEvolvable(
        critic_network,
        critic_input_tensor,
        secondary_input_tensor=secondary_input_tensor,
    )

    # Initialize the matd3 agent
    matd3 = MATD3(
        state_dims=state_dims,
        action_dims=action_dims,
        one_hot=one_hot,
        n_agents=2,
        agent_ids=["agent_0", "agent_1"],
        max_action=[[1], [1]],
        min_action=[[-1], [-1]],
        discrete_actions=True,
        actor_networks=[actor_network, copy.deepcopy(actor_network)],
        critic_networks=[
            [critic_network, copy.deepcopy(critic_network)],
            [copy.deepcopy(critic_network), copy.deepcopy(critic_network)],
        ],
    )

    # Save the checkpoint to a file
    checkpoint_path = Path(tmpdir) / "checkpoint.pth"
    matd3.save_checkpoint(checkpoint_path)

    # Create new agent object
    new_matd3 = MATD3.load(checkpoint_path)

    # Check if properties and weights are loaded correctly
    assert new_matd3.state_dims == matd3.state_dims
    assert new_matd3.action_dims == matd3.action_dims
    assert new_matd3.one_hot == matd3.one_hot
    assert new_matd3.n_agents == matd3.n_agents
    assert new_matd3.agent_ids == matd3.agent_ids
    assert new_matd3.min_action == matd3.min_action
    assert new_matd3.max_action == matd3.max_action
    assert new_matd3.net_config == matd3.net_config
    assert new_matd3.lr_actor == matd3.lr_actor
    assert new_matd3.lr_critic == matd3.lr_critic
    for (
        new_actor,
        new_actor_target,
        new_critic_1,
        new_critic_target_1,
        new_critic_2,
        new_critic_target_2,
        actor,
        actor_target,
        critic_1,
        critic_target_1,
        critic_2,
        critic_target_2,
    ) in zip(
        new_matd3.actors,
        new_matd3.actor_targets,
        new_matd3.critics_1,
        new_matd3.critic_targets_1,
        new_matd3.critics_2,
        new_matd3.critic_targets_2,
        matd3.actors,
        matd3.actor_targets,
        matd3.critics_1,
        matd3.critic_targets_1,
        matd3.critics_2,
        matd3.critic_targets_2,
    ):
        assert isinstance(new_actor, nn.Module)
        assert isinstance(new_actor_target, nn.Module)
        assert isinstance(new_critic_1, nn.Module)
        assert isinstance(new_critic_target_1, nn.Module)
        assert isinstance(new_critic_2, nn.Module)
        assert isinstance(new_critic_target_2, nn.Module)
        assert str(new_actor.to("cpu").state_dict()) == str(actor.state_dict())
        assert str(new_actor_target.to("cpu").state_dict()) == str(
            actor_target.state_dict()
        )
        assert str(new_critic_1.to("cpu").state_dict()) == str(critic_1.state_dict())
        assert str(new_critic_target_1.to("cpu").state_dict()) == str(
            critic_target_1.state_dict()
        )
        assert str(new_critic_2.to("cpu").state_dict()) == str(critic_2.state_dict())
        assert str(new_critic_target_2.to("cpu").state_dict()) == str(
            critic_target_2.state_dict()
        )
    assert new_matd3.batch_size == matd3.batch_size
    assert new_matd3.learn_step == matd3.learn_step
    assert new_matd3.gamma == matd3.gamma
    assert new_matd3.tau == matd3.tau
    assert new_matd3.mut == matd3.mut
    assert new_matd3.index == matd3.index
    assert new_matd3.scores == matd3.scores
    assert new_matd3.fitness == matd3.fitness
    assert new_matd3.steps == matd3.steps

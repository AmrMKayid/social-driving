import itertools
import math
import random
from collections import deque

import numpy as np
import torch
from gym.spaces import Box, Discrete, Tuple
from sdriving.envs.intersection_env import RoadIntersectionControlEnv
from sdriving.trafficsim.dynamics import CatmullRomSplineAccelerationModel
from sdriving.trafficsim.utils import angle_normalize
from sdriving.trafficsim.vehicle import Vehicle
from sdriving.trafficsim.world import World
from sdriving.agents.model import PPOLidarActorCritic


class RoadIntersectionSplineEnv(RoadIntersectionControlEnv):
    def __init__(
        self,
        accln_control_agent: str,
        **kwargs
    ):
        super().__init__(**kwargs)
        ckpt = torch.load(accln_control_agent, map_location="cpu")
        centralized = ckpt["model"] == "centralized_critic"
        self.accln_control = PPOLidarActorCritic(
            **ckpt["ac_kwargs"], centralized=centralized
        )
        self.accln_control.v = None
        self.accln_control.pi.load_state_dict(ckpt["actor"])
        self.accln_control = self.accln_control.to(self.device)
        self.accln_control_actions_list = [
            torch.as_tensor([[ac]]) for ac in np.arange(-1.5, 1.75, 0.25)
        ]
        
        self.configure_action_list()
        
    def configure_action_list(self):
        self.num_stores_per_rollout = 3
        self.actions_list = [
            torch.as_tensor(ac).unsqueeze(0)
            for ac in itertools.product(
                np.arange(-0.75, 0.76, 0.25), np.arange(-0.75, 0.76, 0.25)
            )
        ]

    def get_action_space(self):
        return Discrete(len(self.actions_list))
    
    def post_process_rewards(self, rewards, now_dones):
        return
    
    def get_observation_space(self):
        return Box(
            low=np.array([-1.0, -1.0, -1.0, -1.0, 0.0, 0.0]),
            high=np.array([1.0, 1.0, 1.0, 1.0, 1.0, 1.0]),
        )
    
    def transform_state_action(self, actions, states, timesteps):
        nactions = {}
        nstates = {}
        extras = {}
        for id in self.get_agent_ids_list():
            # actions --> Goal State for MPC
            # states  --> Start State for MPC
            # extras  --> None if using MPC, else tuple of
            #             nominal states, actions
            (
                nactions[id],
                nstates[id],
                extras[id],
            ) = self.transform_state_action_single_agent(
                id, actions[id], states[id], timesteps
            )
        return nactions, nstates, extras

    def transform_state_action_single_agent(
        self, a_id: str, action: torch.Tensor, state, timesteps: int
    ):
        agent = self.agents[a_id]["vehicle"]

        x, y = agent.position
        v = agent.speed
        t = agent.orientation

        start_state = torch.as_tensor([x, y, v, t])
        action = self.accln_control_actions_list[action]
        dynamics = self.agents[a_id]["dynamics"]
        nominal_states = [start_state.unsqueeze(0)]
        nominal_actions = [action]

        for _ in range(timesteps):
            start_state = nominal_states[-1]
            new_state = dynamics(start_state, action)
            nominal_states.append(new_state.cpu())
            nominal_actions.append(action)

        nominal_states, nominal_actions = (
            torch.cat(nominal_states),
            torch.cat(nominal_actions),
        )
        na = torch.zeros(4)
        ns = torch.zeros(4)
        ex = (nominal_states, nominal_actions)

        self.curr_actions[a_id] = action[0]

        return na, ns, ex
    
    def add_vehicle(
        self,
        a_id,
        rname,
        pos,
        v_lim,
        orientation,
        dest,
        dest_orientation,
        dynamics_model=CatmullRomSplineAccelerationModel,
        dynamics_kwargs={},
    ):
        ret_val = super().add_vehicle(
            a_id, rname, pos, v_lim, orientation, dest, dest_orientation,
            CatmullRomSplineAccelerationModel, dynamics_kwargs
        )
        self.agents[a_id]["track_point"] = 0
        self.agents[a_id]["previous_start_point"] = self.agents[a_id]["vehicle"].position
        # Add a dummy start point
        self.agents[a_id]["track"] = [
            self.get_dummy_point(self.agents[a_id]["previous_start_point"]),
            self.agents[a_id]["previous_start_point"].unsqueeze(0)
        ]
        return ret_val

    def get_dummy_point(self, pt: torch.Tensor):
        # This point lies in one of the roads not in a gray area
        x, y = pt
        if x > self.width / 2:
            return torch.as_tensor([self.length + self.width / 2, y]).unsqueeze(0)
        elif x < -self.width / 2:
            return torch.as_tensor([-(self.length + self.width / 2), y]).unsqueeze(0)
        elif y > self.width / 2:
            return torch.as_tensor([x, self.length + self.width / 2]).unsqueeze(0)
        elif y < -self.width / 2:
            return torch.as_tensor([x, -(self.length + self.width / 2)]).unsqueeze(0)
        
    def get_state_single_agent(self, a_id):
        if self.agents[a_id]["track_point"] >= len(self.agents[a_id]["intermediate_goals"]):
            return None
        next_point = self.agents[a_id]["intermediate_goals"][self.agents[a_id]["track_point"]][:2]
        self.agents[a_id]["track_point"] += 1
        lw = self.length + self.width / 2
        return torch.cat([
            self.agents[a_id]["previous_start_point"] / lw,
            next_point / lw,
            torch.as_tensor([1 / self.width, 1 / self.length])
        ])
    
    def get_internal_state(self):
        agent_ids = self.get_agent_ids_list()
        states = {}
        for a_id in agent_ids:
            states[a_id] = super().get_state_single_agent(a_id)
        self.prev_state = states
        return self.prev_state
    
    def distance_reward_function(self, agent):
        # FIXME: A bug prevents the original version
        dist = agent["vehicle"].distance_from_destination()
        return dist / (agent["straight_distance"] * self.horizon)
    
    def step(self, actions: dict, **kwargs):
        completed = False
        for a_id, action in actions.items():
            pt = self.agents[a_id]["intermediate_goals"][self.agents[a_id]["track_point"] - 1][:2][None, :]
            deviation = self.actions_list[action] * self.width / 2
            pt += deviation
            self.agents[a_id]["previous_start_point"] = pt[0]
            self.agents[a_id]["track"].append(pt)
            
            # 1 extra for dummy start
            if len(self.agents[a_id]["track"]) == len(self.agents[a_id]["intermediate_goals"]) + 2:
                completed = True
                self.agents[a_id]["track"].append(
                    self.get_dummy_point(pt[0])
                )
                track = torch.cat(self.agents[a_id]["track"], dim=0)
                self.agents[a_id]["dynamics"].register_track(track, dummy_point=True)
                
        if not completed:
            return (self.get_state(), 0.0, False, {})
        
        rewards = {a_id: 0.0 for a_id in self.get_agent_ids_list()}
        done = False
        while not done:
            states = self.get_internal_state()
            actions = dict()
            for a_id, obs in states.items():
                action = self.accln_control.act(obs, True)
                actions[a_id] = action
            _, rew, dones, _ = super().step(actions, **kwargs)
            for a_id, r in rew.items():
                rewards[a_id] += r
            done = dones["__all__"]
            

        return (None, rewards, True, {})


class RoadIntersectionLeftRightControlEnv(RoadIntersectionSplineEnv):
    def configure_action_list(self):
        self.num_stores_per_rollout = 1
        self.actions_list = [
            torch.as_tensor(ac) for ac in [-0.5, 0.0, 0.5]
        ]
    
    def post_process_rewards(self, rewards, now_dones):
        return
    
    def get_observation_space(self):
        return Box(
            low=np.array([-1.0, -1.0, -1.0]),
            high=np.array([1.0, 1.0, 1.0]),
        )
    
    def get_state_single_agent(self, a_id):
        lw = self.length + self.width / 2
        return torch.cat([
            self.agents[a_id]["vehicle"].position / lw,
            self.agents[a_id]["vehicle"].orientation.unsqueeze(0) / math.pi
        ])
    
    def step(self, actions: dict, **kwargs):
        for a_id, action in actions.items():
            coord = (int(self.agents[a_id]["road name"][-1]) + 1) % 2
            for pt in self.agents[a_id]["intermediate_goals"]:
                pt = pt[:2].clone()
                deviation = self.actions_list[action] * self.width / 2
                pt[coord] = pt[coord] + deviation
                self.agents[a_id]["track"].append(pt.unsqueeze(0))

            self.agents[a_id]["track"].append(self.get_dummy_point(self.agents[a_id]["track"][-1][0]))
            track = torch.cat(self.agents[a_id]["track"], dim=0)
            self.agents[a_id]["dynamics"].register_track(track, dummy_point=True)
        
        rewards = {a_id: 0.0 for a_id in self.get_agent_ids_list()}
        done = False
        while not done:
            states = self.get_internal_state()
            actions = dict()
            for a_id, obs in states.items():
                action = self.accln_control.act(obs, True)
                actions[a_id] = action
            _, rew, dones, _ = RoadIntersectionControlEnv.step(self, actions, **kwargs)
            for a_id, r in rew.items():
                rewards[a_id] += r
            done = dones["__all__"]
            

        return (None, rewards, True, {})
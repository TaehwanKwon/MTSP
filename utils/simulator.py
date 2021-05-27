
import os
import sys
sys.path.append(os.path.abspath( os.path.join(os.path.dirname(__file__), "..")))

# os.environ['OMP_NUM_THREADS'] = '1'
# os.environ['MKL_NUM_THREADS'] = '1'
# os.environ['MKL_SERIAL'] = 'YES'

from envs.mtsp_simple import MTSP, MTSPSimple
from models.gnn import Model, _get_argmax_action

import numpy as np
import torch
import torch.multiprocessing as mp
import copy

from threading import Thread

import logging
logger = logging.getLogger(__name__)

import time

def get_data(idx, config, q_data, q_data_argmax, q_count, q_eps, q_flag_models, q_model):
    device = idx % torch.cuda.device_count()
    model = Model(config, device).to(device)

    env = eval(f"{config['env']['name']}(config['env'])")
    s = env.reset()

    num_collection = 100

    while True:
        if q_count.qsize() > 0:
            count = q_count.get()
            eps = q_eps.get(); q_eps.put(eps)

            if count >= num_collection:
                count = count - num_collection
                _num_collection = num_collection
                q_count.put(count)
            else:
                _num_collection = count
                count = 0

            flag_models = q_flag_models.get()
            if flag_models[idx]:
                flag_models[idx] = False
                q_flag_models.put(flag_models)
            
                state_dict_cpu = q_model.get()
                q_model.put(state_dict_cpu)
                state_dict_gpu = {key: state_dict_cpu[key].to(device) for key in state_dict_cpu}
                model.load_state_dict(state_dict_gpu)
            else:
                q_flag_models.put(flag_models)
            for _ in range(_num_collection):
                _time_test = time.time()
                if np.random.rand() < eps:
                    action = model.action(s, softmax=False)
                else:
                    action = model.action(s, softmax=False)
                time_test = time.time() - _time_test
                #print(f"time_test: {time_test}")
                s_next, reward, done = env.step(action['list'])
                sards = (s, action['numpy'], reward, done, s_next)
                q_data.put(sards)

                if done:
                    s = env.reset()
                else:
                    s = s_next
        
        elif q_data_argmax.qsize() > 0:
            idx_data, data_argmax =  q_data_argmax.get()
            _done_tuple, _state_next_tuple, _action = data_argmax
            argmax_action_list = _get_argmax_action(model, _done_tuple, _state_next_tuple, _action)
            q_data.put( (idx_data, argmax_action_list) )
        time.sleep(1e-3)

def get_data2(idx, config, q_data, q_count, q_eps, q_flag_models, q_model):
    
    model = model_shared

    env = eval(f"{config['env']['name']}(config['env'])")
    s = env.reset()
    s_prev,  action_prev, reward_prev, done_prev = None, None, None, None

    num_collection = 100

    while True:
        count = q_count.get()
        eps = q_eps.get(); q_eps.put(eps)

        if count >= num_collection:
            count = count - num_collection
            _num_collection = num_collection
            q_count.put(count)
        else:
            _num_collection = count
            count = 0

        while _num_collection > 0:
            if done_prev:
                action = None
            else:
                if np.random.rand() < eps:
                    #action = model.random_action(s)
                    action = model.action(s, softmax=True)
                else:
                    _time_test = time.time()
                    action = model.action(s, softmax=False)
                    time_test = time.time() - _time_test
                    #print(f"time_test: {time_test}")
                s_next, reward, done = env.step(action['list'])

            if not (s_prev is None or action_prev is None or reward_prev is None or done_prev is None):
                sardsa = (s_prev, action_prev['numpy'], reward_prev, done_prev, s, action)
                q_data.put(sardsa)
                _num_collection -= 1

            if done_prev:
                s = env.reset()
                s_prev,  action_prev, reward_prev, done_prev = None, None, None, None
            else:
                s_prev = s
                s = s_next
                action_prev = action
                reward_prev = reward
                done_prev = done

class Simulator:
    def __init__(self, config, model):
        self.config = config
        self.model = model # Should be sheard by model.shared_memory()

        self.q_data = mp.Queue()
        self.q_data_argmax = mp.Queue()
        self.q_count = mp.Queue()
        self.q_eps = mp.Queue()
        self.q_model = mp.Queue()
        self.q_flag_models = mp.Queue()

        self.procs = list()
        for idx in range(self.config['learning']['num_processes']):
            target_func = None
            learning_algorithm = self.config['learning']['algorithm']
            if learning_algorithm == 'optimal_q_learning':
                target_func = get_data 
            elif learning_algorithm == 'sarsa':
                target_func = get_data2

            assert not target_func is None, f"Invalid algorithm is set for learning: {learning_algorithm}"

            proc = mp.Process(
                target = target_func, 
                args = (idx, self.config, self.q_data, self.q_data_argmax, self.q_count, self.q_eps, self.q_flag_models, self.q_model)
                )
            proc.start()
            self.procs.append(proc)

    def get_eps(self):
        eps_end = self.config['learning']['eps']['end']
        eps_add = self.config['learning']['eps']['add']
        half_life = self.config['learning']['eps']['half_life']
        eps = eps_end +  eps_add * half_life / (half_life + self.model.step_train)
        return eps

    def get_state_dict_cpu(self):
        state_dict = self.model.state_dict()
        state_dict = { key: state_dict[key].cpu() for key in state_dict }

        return state_dict

    def save_to_replay_buffer(self, size):
        num_data = size
        eps = self.get_eps()
        state_dict_cpu = self.get_state_dict_cpu()

        self.q_count.put(size)
        self.q_eps.put(eps)
        self.q_model.put(state_dict_cpu)
        self.q_flag_models.put( [True for _ in range(self.config['learning']['num_processes'])] )

        while num_data > 0:
            if num_data % 100 == 0:
                logger.debug(f"collecting data.. {num_data} are left")
                #print(f"collecting data.. {num_data} are left")
            if self.q_data.qsize() > 0 :
                sards = self.q_data.get()
                self.model.add_to_replay_buffer(sards)
                num_data = num_data - 1
            else:
                time.sleep(1e-3)
        self.q_eps.get()

    def terminate(self):
        for proc in self.procs:
            proc.kill()





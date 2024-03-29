#!/usr/bin/env python

import json
import logging
import os
import shutil
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Thread
from typing import cast, Dict, Iterator, List

import numpy as np

import yaml

from aido_schemas import (EpisodeStart, protocol_agent, protocol_scenario_maker, protocol_simulator, RobotObservations,
                          RobotPerformance, RobotState, Scenario, SetMap, SetRobotCommands, SimulationState, SpawnRobot,
                          Step, GetRobotState, GetRobotObservations)
from aido_schemas.utils import TimeTracker
from aido_schemas.utils_drawing import read_and_draw
from aido_schemas.utils_video import make_video1
from duckietown_world.rules import RuleEvaluationResult
from duckietown_world.rules.rule import EvaluatedMetric
from zuper_commons.text import indent
from zuper_ipce import ipce_from_object, object_from_ipce
from zuper_nodes.structures import RemoteNodeAborted
from zuper_nodes_wrapper.wrapper_outside import ComponentInterface, MsgReceived
from zuper_typing.subcheck import can_be_used_as2

logging.basicConfig()
logger = logging.getLogger('launcher')
logger.setLevel(logging.DEBUG)


@dataclass
class MyConfig:
    episode_length_s: float
    min_episode_length_s: float
    seed: int
    physics_dt: float
    episodes_per_scenario: int
    max_failures: int

    agent_in: str
    agent_out: str
    sim_in: str
    sim_out: str
    sm_in: str
    sm_out: str

    timeout_initialization: int
    timeout_regular: int


def main(cie, log_dir, attempts):
    config_ = env_as_yaml('experiment_manager_parameters')
    logger.info('parameters:\n\n%s' % config_)
    config = cast(MyConfig, object_from_ipce(config_, MyConfig))

    # first open all fifos
    agent_ci = ComponentInterface(config.agent_in, config.agent_out,
                                  expect_protocol=protocol_agent, nickname="agent",
                                  timeout=config.timeout_regular)
    agents = [agent_ci]
    sim_ci = ComponentInterface(config.sim_in, config.sim_out,
                                expect_protocol=protocol_simulator, nickname="simulator",
                                timeout=config.timeout_regular)
    sm_ci = ComponentInterface(config.sm_in, config.sm_out,
                               expect_protocol=protocol_scenario_maker, nickname="scenario_maker",
                               timeout=config.timeout_regular)

    # then check compatibility
    # so that everything fails gracefully in case of error

    sm_ci._get_node_protocol(timeout=config.timeout_initialization)
    sim_ci._get_node_protocol(timeout=config.timeout_initialization)
    agent_ci._get_node_protocol(timeout=config.timeout_initialization)

    check_compatibility_between_agent_and_sim(agent_ci, sim_ci)

    attempt_i = 0
    per_episode = {}
    stats = {}
    try:

        nfailures = 0

        sim_ci.write_topic_and_expect_zero('seed', config.seed)
        agent_ci.write_topic_and_expect_zero('seed', config.seed)

        episodes = get_episodes(sm_ci, episodes_per_scenario=config.episodes_per_scenario,
                                seed=config.seed)

        while episodes:

            if nfailures >= config.max_failures:
                msg = 'Too many failures: %s' % nfailures
                raise Exception(msg)  # XXX

            episode_spec = episodes[0]
            episode_name = episode_spec.episode_name

            logger.info('Starting episode %s' % episode_name)

            dn_final = os.path.join(log_dir, episode_name)

            if os.path.exists(dn_final):
                shutil.rmtree(dn_final)

            dn = os.path.join(attempts, episode_name + '.attempt%s' % attempt_i)
            if os.path.exists(dn):
                shutil.rmtree(dn)

            if not os.path.exists(dn):
                os.makedirs(dn)
            fn = os.path.join(dn, 'log.gs2.cbor')

            fn_tmp = fn + '.tmp'
            fw = open(fn_tmp, 'wb')

            agent_ci.cc(fw)
            sim_ci.cc(fw)

            logger.info('Now running episode')

            num_playable = len([_ for _ in episode_spec.scenario.robots.values() if _.playable])
            if num_playable != len(agents):
                msg = f'The scenario requires {num_playable} robots, but I only know {len(agents)} agents.'
                raise Exception(msg)  # XXX
            try:
                length_s = run_episode(sim_ci,
                                       agents,
                                       episode_name=episode_name,
                                       scenario=episode_spec.scenario,
                                       episode_length_s=config.episode_length_s,
                                       physics_dt=config.physics_dt)
                logger.info('Finished episode %s' % episode_name)

            except:
                msg = 'Anomalous error from run_episode():\n%s' % traceback.format_exc()
                logger.error(msg)
                raise
            finally:
                fw.close()
                os.rename(fn_tmp, fn)

            # output = os.path.join(dn, 'visualization')
            logger.info('Now creating visualization and analyzing statistics.')
            logger.warning('This might take a LONG time.')

            with notice_thread("Visualization", 2):
                evaluated = read_and_draw(fn, dn)
            logger.info('Finally visualization is done.')

            stats = {}
            for k, evr in evaluated.items():
                assert isinstance(evr, RuleEvaluationResult)
                for m, em in evr.metrics.items():
                    assert isinstance(em, EvaluatedMetric)
                    assert isinstance(m, tuple)
                    if m:
                        M = "/".join(m)
                    else:
                        M = k
                    stats[M] = float(em.total)
            per_episode[episode_name] = stats

            if length_s >= config.min_episode_length_s:
                logger.info('%1.f s are enough' % length_s)
                episodes.pop(0)

                out_video = os.path.join(dn, 'camera.mp4')
                with notice_thread("Make video", 2):
                    make_video1(fn, out_video)

                os.rename(dn, dn_final)
            else:
                logger.error('episode too short with %1.f s < %.1f s' % (length_s, config.min_episode_length_s))
                nfailures += 1
            attempt_i += 1
    except dc.InvalidSubmission:
        raise
    except BaseException as e:
        msg = 'Anomalous error while running episodes:'
        msg += '\n\n' + indent(traceback.format_exc(), ' > ')
        logger.error(msg)
        raise dc.InvalidEvaluator(msg) from e

    finally:
        agent_ci.close()
        sim_ci.close()
        logger.info('Simulation done.')

    cie.set_score('per-episodes', per_episode)

    for k in list(stats):
        values = [_[k] for _ in per_episode.values()]
        cie.set_score('%s_mean' % k, float(np.mean(values)))
        cie.set_score('%s_median' % k, float(np.median(values)))
        cie.set_score('%s_min' % k, float(np.min(values)))
        cie.set_score('%s_max' % k, float(np.max(values)))


@contextmanager
def notice_thread(msg, interval):
    stop = False
    t0 = time.time()
    t = Thread(target=notice_thread_child, args=(msg, interval, lambda: stop))
    t.start()
    try:

        yield

    finally:
        t1 = time.time()
        delta = t1 - t0
        logger.info(f'{msg}: took {delta} seconds.')
        stop = True
        logger.info('waiting for thread to finish')
        t.join()


def notice_thread_child(msg, interval, stop_condition):
    t0 = time.time()
    while not stop_condition():
        delta = time.time() - t0
        logger.info(msg + '(running for %d seconds)' % delta)
        time.sleep(interval)
    # logger.info('notice_thread_child finishes')


def run_episode(sim_ci: ComponentInterface,
                agents: List[ComponentInterface],
                physics_dt: float,
                episode_name, scenario: Scenario,
                episode_length_s: float) -> float:
    ''' returns number of steps '''

    # clear simulation
    sim_ci.write_topic_and_expect_zero('clear')
    # set map data
    sim_ci.write_topic_and_expect_zero('set_map', SetMap(map_data=scenario.environment))

    # spawn robot
    for robot_name, robot_conf in scenario.robots.items():
        sim_ci.write_topic_and_expect_zero('spawn_robot',
                                           SpawnRobot(robot_name=robot_name, configuration=robot_conf.configuration,
                                                      playable=robot_conf.playable))

    # start episode
    sim_ci.write_topic_and_expect_zero('episode_start', EpisodeStart(episode_name))

    for agent in agents:
        agent.write_topic_and_expect_zero('episode_start', EpisodeStart(episode_name))

    current_sim_time = 0.0

    # for now, fixed timesteps

    steps = 0

    playable_robots = [_ for _ in scenario.robots if scenario.robots[_].playable]
    not_playable_robots = [_ for _ in scenario.robots if not scenario.robots[_].playable]
    playable_robots2agent: Dict[str, ComponentInterface] = {_: v for _, v in zip(playable_robots, agents)}

    while True:
        if current_sim_time >= episode_length_s:
            logger.info('Reached %1.f seconds. Finishing. ' % episode_length_s)
            break

        tt = TimeTracker(steps)
        t_effective = current_sim_time
        for robot_name in playable_robots:
            agent = playable_robots2agent[robot_name]

            # have this first, so we have something for t = 0
            with tt.measure(f'sim_compute_robot_state-{robot_name}'):
                grs = GetRobotState(robot_name=robot_name, t_effective=t_effective)
                _recv: MsgReceived[RobotState] = \
                    sim_ci.write_topic_and_expect('get_robot_state', grs,
                                                  expect='robot_state')

            with tt.measure(f'sim_compute_performance-{robot_name}'):

                _recv: MsgReceived[RobotPerformance] = \
                    sim_ci.write_topic_and_expect('get_robot_performance',
                                                  robot_name,
                                                  expect='robot_performance')

            with tt.measure(f'sim_render-{robot_name}'):
                gro = GetRobotObservations(robot_name=robot_name, t_effective=t_effective)
                recv: MsgReceived[RobotObservations] = \
                    sim_ci.write_topic_and_expect('get_robot_observations', gro,
                                                  expect='robot_observations')

            with tt.measure(f'agent_compute-{robot_name}'):
                try:
                    agent.write_topic_and_expect_zero('observations', recv.data.observations)
                    r: MsgReceived = agent.write_topic_and_expect('get_commands', expect='commands')

                except BaseException as e:
                    msg = 'Trouble with communication to the agent.'
                    raise dc.InvalidSubmission(msg) from e

            with tt.measure('set_robot_commands'):
                commands = SetRobotCommands(robot_name=robot_name, commands=r.data, t_effective=t_effective)
                sim_ci.write_topic_and_expect_zero('set_robot_commands', commands)

        for robot_name in not_playable_robots:
            with tt.measure(f'sim_compute_robot_state-{robot_name}'):
                rs = GetRobotState(robot_name=robot_name, t_effective=t_effective)
                _recv: MsgReceived[RobotState] = \
                    sim_ci.write_topic_and_expect('get_robot_state', rs,
                                                  expect='robot_state')

        with tt.measure('sim_compute_sim_state'):

            recv: MsgReceived[SimulationState] = \
                sim_ci.write_topic_and_expect('get_sim_state', expect='sim_state')

            sim_state: SimulationState = recv.data
            if sim_state.done:
                logger.info(f'Breaking because of simulator ({sim_state.done_code} - {sim_state.done_why}')
                break

        with tt.measure('sim_physics'):
            current_sim_time += physics_dt
            sim_ci.write_topic_and_expect_zero('step', Step(current_sim_time))

        log_timing_info(tt, sim_ci)

    return current_sim_time


def log_timing_info(tt, sim_ci: ComponentInterface):
    ipce = ipce_from_object(tt)
    msg = {'compat': ['aido2'], 'topic': 'timing_information', 'data': ipce}
    j = sim_ci._serialize(msg)
    sim_ci._cc.write(j)
    sim_ci._cc.flush()


def check_compatibility_between_agent_and_sim(agent_ci: ComponentInterface, sim_ci: ComponentInterface):
    type_observations_sim = sim_ci.node_protocol.outputs['robot_observations'].__annotations__['observations']
    logger.info(f'Simulation provides observations {type_observations_sim}')
    type_commands_sim = sim_ci.node_protocol.inputs['set_robot_commands'].__annotations__['commands']
    logger.info(f'Simulation requires commands {type_commands_sim}')

    if agent_ci.node_protocol is None:
        msg = 'Cannot check compatibility of interfaces because the agent does not implement reflection.'
        logger.warning(msg)

        agent_ci.expect_protocol.outputs['commands'] = type_commands_sim
        agent_ci.expect_protocol.inputs['observations'] = type_observations_sim

        return

    type_observations_agent = agent_ci.node_protocol.inputs['observations']
    logger.info(f'Agent requires observations {type_observations_agent}')

    type_commands_agent = agent_ci.node_protocol.outputs['commands']
    logger.info(f'Agent provides commands {type_commands_agent}')

    r = can_be_used_as2(type_observations_sim, type_observations_agent)
    if not r.result:
        msg = 'Observations mismatch: %s' % r
        logger.error(msg)
        raise Exception(msg)
    r = can_be_used_as2(type_commands_agent, type_commands_sim)
    if not r:
        msg = 'Commands mismatch: %s' % r
        logger.error(msg)
        raise Exception(msg)


@dataclass
class EpisodeSpec:
    episode_name: str
    scenario: Scenario


def get_episodes(sm_ci: ComponentInterface, episodes_per_scenario: int, seed: int) -> List[EpisodeSpec]:
    sm_ci.write_topic_and_expect_zero('seed', seed)

    def iterate_scenarios() -> Iterator[Scenario]:
        while True:
            recv = sm_ci.write_topic_and_expect('next_scenario')
            if recv.topic == 'finished':
                sm_ci.close()
                break
            else:
                yield recv.data

    episodes = []
    for scenario in iterate_scenarios():
        scenario_name = scenario.scenario_name
        logger.info(f'Received scenario {scenario}')
        for i in range(episodes_per_scenario):
            episode_name = f'{scenario_name}-{i}'
            es = EpisodeSpec(episode_name=episode_name, scenario=scenario)
            episodes.append(es)
    return episodes


def env_as_yaml(name):
    environment = os.environ.copy()
    if not name in environment:
        msg = 'Could not find variable "%s"; I know:\n%s' % (name, json.dumps(environment, indent=4))
        raise Exception(msg)
    v = environment[name]
    try:
        return yaml.load(v, Loader=yaml.SafeLoader)
    except Exception as e:
        msg = 'Could not load YAML: %s\n\n%s' % (e, v)
        raise Exception(msg)


import duckietown_challenges as dc


def wrap(cie: dc.ChallengeInterfaceEvaluator):
    d = cie.get_tmp_dir()

    logdir = os.path.join(d, 'episodes')

    attempts = os.path.join(d, 'attempts')
    if not os.path.exists(logdir):
        os.makedirs(logdir)
    if not os.path.exists(attempts):
        os.makedirs(attempts)
    try:
        main(cie, logdir, attempts)
        cie.set_score('simulation-passed', 1)
    finally:
        cie.info('saving files')
        cie.set_evaluation_dir('episodes', logdir)

    cie.info('score() terminated gracefully.')


if __name__ == '__main__':
    with dc.scoring_context() as cie:
        try:
            wrap(cie)
        except RemoteNodeAborted as e:
            msg = 'It appears that one of the remote nodes has aborted.\n' \
                  'I will wait 10 seconds before aborting myself so that its\n' \
                  'error will be detected by the evaluator rather than mine.'
            msg += f'\n\n{traceback.format_exc()}'
            cie.error(msg)
            time.sleep(10)
            raise

from typing import Any

import numpy as np
from gym import Space
from gym.spaces import Tuple, Box
from numba import njit
from rlgym.utils import ObsBuilder
from rlgym.utils.action_parsers import DefaultAction
from rlgym.utils.common_values import BOOST_LOCATIONS, BLUE_TEAM, ORANGE_TEAM
from rlgym.utils.gamestates import GameState, PlayerData

from rocket_learn.utils.batched_obs_builder import BatchedObsBuilder
from rocket_learn.utils.gamestate_encoding import encode_gamestate
from rocket_learn.utils.gamestate_encoding import StateConstants as SC


class NectoObsOLD(ObsBuilder):
    _boost_locations = np.array(BOOST_LOCATIONS)
    _invert = np.array([1] * 5 + [-1, -1, 1] * 5 + [1] * 4)
    _norm = np.array([1.] * 5 + [2300] * 6 + [1] * 6 + [5.5] * 3 + [1] * 4)

    def __init__(self, n_players=6, tick_skip=8):
        super().__init__()
        self.n_players = n_players
        self.demo_timers = None
        self.boost_timers = None
        self.current_state = None
        self.current_qkv = None
        self.current_mask = None
        self.tick_skip = tick_skip

    def reset(self, initial_state: GameState):
        self.demo_timers = np.zeros(self.n_players)
        self.boost_timers = np.zeros(len(initial_state.boost_pads))
        # self.current_state = initial_state

    def _maybe_update_obs(self, state: GameState):
        if state == self.current_state:  # No need to update
            return

        if self.boost_timers is None:
            self.reset(state)
        else:
            self.current_state = state

        qkv = np.zeros((1, 1 + self.n_players + len(state.boost_pads), 24))  # Ball, players, boosts

        # Add ball
        n = 0
        ball = state.ball
        qkv[0, 0, 3] = 1  # is_ball
        qkv[0, 0, 5:8] = ball.position
        qkv[0, 0, 8:11] = ball.linear_velocity
        qkv[0, 0, 17:20] = ball.angular_velocity

        # Add players
        n += 1
        demos = np.zeros(self.n_players)  # Which players are currently demoed
        for player in state.players:
            if player.team_num == BLUE_TEAM:
                qkv[0, n, 1] = 1  # is_teammate
            else:
                qkv[0, n, 2] = 1  # is_opponent
            car_data = player.car_data
            qkv[0, n, 5:8] = car_data.position
            qkv[0, n, 8:11] = car_data.linear_velocity
            qkv[0, n, 11:14] = car_data.forward()
            qkv[0, n, 14:17] = car_data.up()
            qkv[0, n, 17:20] = car_data.angular_velocity
            qkv[0, n, 20] = player.boost_amount
            #             qkv[0, n, 21] = player.is_demoed
            demos[n - 1] = player.is_demoed  # Keep track for demo timer
            qkv[0, n, 22] = player.on_ground
            qkv[0, n, 23] = player.has_flip
            n += 1

        # Add boost pads
        n = 1 + self.n_players
        boost_pads = state.boost_pads
        qkv[0, n:, 4] = 1  # is_boost
        qkv[0, n:, 5:8] = self._boost_locations
        qkv[0, n:, 20] = 0.12 + 0.88 * (self._boost_locations[:, 2] > 72)  # Boost amount
        #         qkv[0, n:, 21] = boost_pads

        # Boost and demo timers
        new_boost_grabs = (boost_pads == 1) & (self.boost_timers == 0)  # New boost grabs since last frame
        self.boost_timers[new_boost_grabs] = 0.4 + 0.6 * (self._boost_locations[new_boost_grabs, 2] > 72)
        self.boost_timers *= boost_pads  # Make sure we have zeros right
        qkv[0, 1 + self.n_players:, 21] = self.boost_timers
        self.boost_timers -= self.tick_skip / 1200  # Pre-normalized, 120 fps for 10 seconds
        self.boost_timers[self.boost_timers < 0] = 0

        new_demos = (demos == 1) & (self.demo_timers == 0)
        self.demo_timers[new_demos] = 0.3
        self.demo_timers *= demos
        qkv[0, 1: 1 + self.n_players, 21] = self.demo_timers
        self.demo_timers -= self.tick_skip / 1200
        self.demo_timers[self.demo_timers < 0] = 0

        # Store results
        self.current_qkv = qkv / self._norm
        mask = np.zeros((1, qkv.shape[1]))
        mask[0, 1 + len(state.players):1 + self.n_players] = 1
        self.current_mask = mask

    def build_obs(self, player: PlayerData, state: GameState, previous_action: np.ndarray) -> Any:
        if self.boost_timers is None:
            return np.zeros(0)  # Obs space autodetect, make Aech happy
        self._maybe_update_obs(state)
        invert = player.team_num == ORANGE_TEAM

        qkv = self.current_qkv.copy()
        mask = self.current_mask.copy()

        main_n = state.players.index(player) + 1
        qkv[0, main_n, 0] = 1  # is_main
        if invert:
            qkv[0, :, (1, 2)] = qkv[0, :, (2, 1)]  # Swap blue/orange
            qkv *= self._invert  # Negate x and y values

        # TODO left-right normalization (always pick one side)

        q = qkv[0, main_n, :]
        q = np.expand_dims(np.concatenate((q, previous_action), axis=0), axis=(0, 1))
        # kv = np.delete(qkv, main_n, axis=0)  # Delete main? Watch masking
        kv = qkv

        # With EARLPerceiver we can use relative coords+vel(+more?) for key/value tensor, might be smart
        kv[0, :, 5:11] -= q[0, 0, 5:11]
        return q, kv, mask


IS_SELF, IS_MATE, IS_OPP, IS_BALL, IS_BOOST = range(5)
POS = slice(5, 8)
LIN_VEL = slice(8, 11)
FW = slice(11, 14)
UP = slice(14, 17)
ANG_VEL = slice(17, 20)
BOOST, DEMO, ON_GROUND, HAS_FLIP, HAS_JUMP = range(20, 25)
ACTIONS = range(25, 33)


# BOOST, DEMO, ON_GROUND, HAS_FLIP = range(20, 24)
# ACTIONS = range(24, 32)


class NectoObsBuilder(BatchedObsBuilder):
    _boost_locations = np.array(BOOST_LOCATIONS)
    _invert = np.array([1] * 5 + [-1, -1, 1] * 5 + [1] * 5 + [1] * 30)
    _norm = np.array([1.] * 5 + [2300] * 6 + [1] * 6 + [5.5] * 3 + [1, 10, 1, 1, 1] + [1] * 30)

    def __init__(self, n_players=None, tick_skip=8):
        super().__init__()
        self.n_players = n_players
        self.demo_timers = None
        self.boost_timers = None
        self.current_state = None
        self.current_qkv = None
        self.current_mask = None
        self.tick_skip = tick_skip

    def _reset(self, initial_state: GameState):
        self.demo_timers = np.zeros(len(initial_state.players))
        self.boost_timers = np.zeros(len(initial_state.boost_pads))

    def get_obs_space(self) -> Space:
        players = self.n_players or 6
        entities = 1 + players + len(self._boost_locations)
        return Tuple((
            Box(-np.inf, np.inf, (1, len(self._invert) - 30 + 8)),
            Box(-np.inf, np.inf, (entities, len(self._invert))),
            Box(-np.inf, np.inf, (entities,)),
        ))

    @staticmethod
    def _quats_to_rot_mtx(quats: np.ndarray) -> np.ndarray:
        # From rlgym.utils.math.quat_to_rot_mtx
        w = -quats[:, 0]
        x = -quats[:, 1]
        y = -quats[:, 2]
        z = -quats[:, 3]

        theta = np.zeros((quats.shape[0], 3, 3))

        norm = np.einsum("fq,fq->f", quats, quats)

        sel = norm != 0

        w = w[sel]
        x = x[sel]
        y = y[sel]
        z = z[sel]

        s = 1.0 / norm[sel]

        # front direction
        theta[sel, 0, 0] = 1.0 - 2.0 * s * (y * y + z * z)
        theta[sel, 1, 0] = 2.0 * s * (x * y + z * w)
        theta[sel, 2, 0] = 2.0 * s * (x * z - y * w)

        # left direction
        theta[sel, 0, 1] = 2.0 * s * (x * y - z * w)
        theta[sel, 1, 1] = 1.0 - 2.0 * s * (x * x + z * z)
        theta[sel, 2, 1] = 2.0 * s * (y * z + x * w)

        # up direction
        theta[sel, 0, 2] = 2.0 * s * (x * z + y * w)
        theta[sel, 1, 2] = 2.0 * s * (y * z - x * w)
        theta[sel, 2, 2] = 1.0 - 2.0 * s * (x * x + y * y)

        return theta

    @staticmethod
    def convert_to_relative(q, kv):
        kv[..., POS.start:LIN_VEL.stop] -= q[..., POS.start:LIN_VEL.stop]
        # kv[..., POS] -= q[..., POS]
        forward = q[..., FW]
        theta = np.arctan2(forward[..., 0], forward[..., 1])
        theta = np.expand_dims(theta, axis=-1)
        ct = np.cos(theta)
        st = np.sin(theta)
        xs = kv[..., POS.start:ANG_VEL.stop:3]
        ys = kv[..., POS.start + 1:ANG_VEL.stop:3]
        # Use temp variables to prevent modifying original array
        nx = ct * xs - st * ys
        ny = st * xs + ct * ys
        kv[..., POS.start:ANG_VEL.stop:3] = nx  # x-components
        kv[..., POS.start + 1:ANG_VEL.stop:3] = ny  # y-components

    @staticmethod
    def add_relative_components(q, kv):
        forward = q[..., FW]
        up = q[..., UP]
        left = np.cross(up, forward)

        pitch = np.arctan2(forward[..., 2], np.sqrt(forward[..., 0] ** 2 + forward[..., 1] ** 2))
        yaw = np.arctan2(forward[..., 1], forward[..., 0])
        roll = np.arctan2(left[..., 2], up[..., 2])

        pitch = np.expand_dims(pitch, axis=-1)
        yaw = np.expand_dims(yaw, axis=-1)
        roll = np.expand_dims(roll, axis=-1)

        cr = np.cos(roll)
        sr = np.sin(roll)
        cp = np.cos(pitch)
        sp = np.sin(pitch)
        cy = np.cos(yaw)
        sy = np.sin(yaw)

        # Each of these holds 5 values for each player for each tick
        vals = kv[..., POS.start:ANG_VEL.stop]
        vals[..., POS.start:LIN_VEL.stop] -= q[..., POS.start:LIN_VEL.stop]
        xs = vals[..., 0::3]
        ys = vals[..., 1::3]
        zs = vals[..., 2::3]

        # Rotation matrix with only yaw
        flip_relative_xs = cy * xs - sy * ys
        flip_relative_ys = sy * xs + cy * ys
        flip_relative_zs = zs

        # Now full rotation matrix
        car_relative_xs = cp * cy * xs + (sr * sp * cy - cr * sy) * ys - (cr * sp * cy + sr * sy) * zs
        car_relative_ys = cp * sy * xs + (sr * sp * sy + cr * cy) * ys - (cr * sp * sy - sr * cy) * zs
        car_relative_zs = sp * xs - cp * sr * ys + cp * cr * zs

        all_rows = np.concatenate(
            (flip_relative_xs, flip_relative_ys, flip_relative_zs,
             car_relative_xs, car_relative_ys, car_relative_zs), axis=-1)
        kv[..., ACTIONS.start:] = all_rows

    @staticmethod
    @njit
    def _update_timers(self_boost_timers, self_boost_locations, self_demo_timers, self_tick_skip,
                       boost_states: np.ndarray, demo_states: np.ndarray):
        boost_timers = np.zeros((boost_states.shape[0] + 1, boost_states.shape[1]))
        boost_timers[0, :] = self_boost_timers

        for i in range(1, boost_timers.shape[0]):
            for b in range(boost_timers.shape[1]):
                if boost_states[i, b] == 0:
                    if self_boost_locations[b, 2] > 72:
                        boost_timers[i, b] = 10
                    else:
                        boost_timers[i, b] = 4
                elif i - 1 >= 0 and boost_timers[i - 1, b] > 0:
                    boost_timers[i, b] = max(0, boost_timers[i - 1, b] - self_tick_skip / 120)
        # self.boost_timers = boost_timers[-1, :]

        demo_timers = np.zeros((demo_states.shape[0] + 1, demo_states.shape[1]))
        demo_timers[0, :] = self_demo_timers
        for i in range(1, demo_timers.shape[0]):
            for b in range(demo_timers.shape[1]):
                if demo_states[i, b] == 1:
                    demo_timers[i, b] = 3
                elif i - 1 >= 0 and demo_timers[i - 1, b] > 0:
                    demo_timers[i, b] = max(0, demo_timers[i - 1, b] - self_tick_skip / 120)
        # self.demo_timers = demo_timers[-1, :]

        return boost_timers[1:], demo_timers[1:]

    def batched_build_obs(self, encoded_states: np.ndarray):
        ball_start_index = 3 + GameState.BOOST_PADS_LENGTH
        players_start_index = ball_start_index + GameState.BALL_STATE_LENGTH
        player_length = GameState.PLAYER_INFO_LENGTH

        n_players = (encoded_states.shape[1] - players_start_index) // player_length
        lim_players = n_players if self.n_players is None else self.n_players
        n_entities = lim_players + 1 + 34  # Includes player+ball+boosts

        boost_timers, demo_timers = self._update_timers(self.boost_timers, self._boost_locations,
                                                        self.demo_timers, self.tick_skip,
                                                        encoded_states[:, 3:3 + 34],
                                                        encoded_states[:, players_start_index + 33::player_length])
        self.boost_timers = boost_timers[-1, :]
        self.demo_timers = demo_timers[-1, :]

        # SELECTORS
        sel_players = slice(0, lim_players)
        sel_ball = sel_players.stop
        sel_boosts = slice(sel_ball + 1, None)

        # MAIN ARRAYS
        q = np.zeros((n_players, encoded_states.shape[0], 1, 33))
        kv = np.zeros((n_players, encoded_states.shape[0], n_entities, 25 + 30))
        m = np.zeros((n_players, encoded_states.shape[0], n_entities))  # Mask is shared

        # BALL
        kv[:, :, sel_ball, 3] = 1
        kv[:, :, sel_ball, np.r_[POS, LIN_VEL, ANG_VEL]] = encoded_states[:, ball_start_index: ball_start_index + 9]

        # BOOSTS
        # big_boost_mask = self._boost_locations[:, 2] > 72
        kv[:, :, sel_boosts, IS_BOOST] = 1
        kv[:, :, sel_boosts, POS] = self._boost_locations  # [big_boost_mask]
        kv[:, :, sel_boosts, BOOST] = 1
        kv[:, :, sel_boosts, DEMO] = boost_timers  # [:, big_boost_mask]
        # q[:, :, ACTIONS.stop:] = boost_timers[:, ~big_boost_mask]

        # PLAYERS
        teams = encoded_states[0, players_start_index + 1::player_length]
        kv[:, :, :n_players, IS_MATE] = 1 - teams  # Default team is blue
        kv[:, :, :n_players, IS_OPP] = teams
        for i in range(n_players):
            encoded_player = encoded_states[:,
                             players_start_index + i * player_length: players_start_index + (i + 1) * player_length]

            kv[i, :, i, IS_SELF] = 1
            kv[:, :, i, POS] = encoded_player[:, SC.CAR_POS_X.start: SC.CAR_POS_Z.start + 1]
            kv[:, :, i, LIN_VEL] = encoded_player[:, SC.CAR_LINEAR_VEL_X.start: SC.CAR_LINEAR_VEL_Z.start + 1]
            quats = encoded_player[:, SC.CAR_QUAT_W.start: SC.CAR_QUAT_Z.start + 1]
            rot_mtx = self._quats_to_rot_mtx(quats)
            kv[:, :, i, FW] = rot_mtx[:, :, 0]
            kv[:, :, i, UP] = rot_mtx[:, :, 2]
            kv[:, :, i, ANG_VEL] = encoded_player[:, SC.CAR_ANGULAR_VEL_X.start: SC.CAR_ANGULAR_VEL_Z.start + 1]
            kv[:, :, i, BOOST] = encoded_player[:, SC.BOOST_AMOUNT.start]
            kv[:, :, i, DEMO] = demo_timers[:, i]
            kv[:, :, i, ON_GROUND] = encoded_player[:, SC.ON_GROUND.start]
            kv[:, :, i, HAS_FLIP] = encoded_player[:, SC.HAS_FLIP.start]
            kv[:, :, i, HAS_JUMP] = encoded_player[:, SC.HAS_JUMP.start]

        kv[teams == 1] *= self._invert
        kv[np.argwhere(teams == 1), ..., (IS_MATE, IS_OPP)] = kv[
            np.argwhere(teams == 1), ..., (IS_OPP, IS_MATE)]  # Swap teams

        kv /= self._norm

        for i in range(n_players):
            q[i, :, 0, :HAS_JUMP + 1] = kv[i, :, i, :HAS_JUMP + 1]

        self.add_relative_components(q, kv)
        # self.convert_to_relative(q, kv)
        # kv[:, :, :, 5:11] -= q[:, :, :, 5:11]

        # MASK
        m[:, :, n_players: lim_players] = 1

        return [(q[i], kv[i], m[i]) for i in range(n_players)]

    def add_actions(self, obs: Any, previous_actions: np.ndarray, player_index=None):
        if player_index is None:
            for (q, kv, m), act in zip(obs, previous_actions):
                q[:, 0, ACTIONS] = act
        else:
            q, kv, m = obs[player_index]
            q[:, 0, ACTIONS] = previous_actions


if __name__ == '__main__':
    import rlgym


    class CombinedObs(ObsBuilder):
        def __init__(self, *obsbs):
            super().__init__()
            self.obsbs = obsbs

        def reset(self, initial_state: GameState):
            for obsb in self.obsbs:
                obsb.reset(initial_state)

        def build_obs(self, player: PlayerData, state: GameState, previous_action: np.ndarray) -> Any:
            obss = []
            for obsb in self.obsbs:
                obss.append(obsb.build_obs(player, state, previous_action))
            return obss


    env = rlgym.make(use_injector=True, self_play=True, team_size=3,
                     obs_builder=CombinedObs(NectoObsBuilder(n_players=6), NectoObsOLD()))

    states = []
    actions = [[np.zeros(8)] for _ in range(6)]
    done = False
    obs, info = env.reset(return_info=True)
    obss = [[o] for o in obs]
    states.append(info["state"])
    while not done:
        act = [env.action_space.sample() for _ in range(6)]
        for a, arr in zip(act, actions):
            arr.append(a)
        obs, reward, done, info = env.step(act)
        for os, o in zip(obss, obs):
            os.append(o)
        states.append(info["state"])

    obs_b = NectoObsBuilder(n_players=6)

    enc_states = np.array([encode_gamestate(s) for s in states])
    actions = np.array(actions)

    # FIXME ensure obs corresponds to old obs
    # FIXME ensure reconstructed obs is *exactly* the same as obs
    # reconstructed_obs = obs_b.reset(GameState(enc_states[0].tolist()))
    reconstructed_obs = obs_b.batched_build_obs(enc_states)
    ap = DefaultAction()
    obs_b.add_actions(reconstructed_obs, ap.parse_actions(actions.reshape(-1, 8), None).reshape(actions.shape))

    formatted_obss = []
    for player_obs in obss:
        transposed = tuple(zip(*player_obs))
        obs_tensor = tuple(np.vstack(t) for t in transposed)
        formatted_obss.append(obs_tensor)

    for o0, o1 in zip(formatted_obss, reconstructed_obs):
        for arr0, arr1 in zip(o0, o1):
            if not np.all(arr0 == arr1):
                print("Error")

    print("Hei")

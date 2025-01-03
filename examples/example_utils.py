from __future__ import division, print_function

import sys
import os
import importlib
import numpy as np
import scipy
from scipy import signal
from matplotlib.colors import ListedColormap
import sys
sys.path.insert(0, '../')
from mars.utils import dict2func
import torch
from mars import config, DeterministicFunction, GridWorld, PTPDNet,\
     PTPDNet_Quadratic, PTPDNet_SumOfTwo, Perturb_PosSemi, Perturb_ETH,\
    SumOfTwo_PosSemi, SumOfTwo_ETH, DiffSumOfTwo_ETH
from mars.utils import concatenate_inputs
import pickle 

__all__ = ['build_system', 'InvertedPendulum', 'CartPole', 'VanDerPol', 'LyapunovNetwork', 'compute_roa', 'generate_trajectories', 'save_dict', 'load_dict']

class LyapunovNetwork(DeterministicFunction):
    def __init__(self, input_dim, structure, layer_dims, activations, eps=1e-6,
                initializer=torch.nn.init.xavier_uniform,
                name='lyapunov_network'):
        """
        initializer: a function that takes weights as intput and initialize them
        """
        super(LyapunovNetwork, self).__init__(name=name)
        self.input_dim = input_dim
        self.num_layers = len(layer_dims)
        self.activations = activations
        self.layer_dims = layer_dims
        self.eps = eps
        self.initializer = initializer
        if structure == "eth":
            self.net = PTPDNet(self.input_dim, self.layer_dims, self.activations, self.initializer, self.eps).to(config.device)
            print("Structure of the Lyapunov network: eth")
        elif structure == "quadratic":
            self.net = PTPDNet_Quadratic(self.input_dim, self.layer_dims, self.activations, self.initializer, self.eps).to(config.device)
            print("Structure of the Lyapunov network: quadratic")
        elif structure == "sum_of_two":
            self.net = PTPDNet_SumOfTwo(self.input_dim, self.layer_dims, self.activations, self.initializer, self.eps).to(config.device)
            print("Structure of the Lyapunov network: sum_of_two")
        elif structure == "perturb_pos_semi":
            self.net = Perturb_PosSemi(self.input_dim, self.layer_dims, self.activations, self.initializer).to(config.device)
            print("Structure of the Lyapunov network: perturb_pos_semi")
        elif structure == "perturb_eth":
            self.net = Perturb_ETH(self.input_dim, self.layer_dims, self.activations, self.initializer).to(config.device)
            print("Structure of the Lyapunov network: perturb_eth")
        elif structure == "sum_of_two_pos_semi":
            self.net = SumOfTwo_PosSemi(self.input_dim, self.layer_dims, self.activations, self.initializer).to(config.device)
            print("Structure of the Lyapunov network: sum_of_two_pos_semi")
        elif structure == "sum_of_two_eth":
            self.net = SumOfTwo_ETH(self.input_dim, self.layer_dims, self.activations, self.initializer).to(config.device)
            print("Structure of the Lyapunov network: sum_of_two_eth")
        else:
            raise ValueError('No match found for nn_structure!')

    def eval(self, x):
        return self.net(x)

    def __call__(self, x):
        return self.eval(x)


class Andrea(DeterministicFunction):
    """
    The system proposed by Andrea for the ODE-ROA project
    Parameters. When the system is autonomous, we use a dummy action
    for consistency.
    --------------
    delta: A constant that changes the ROA of the system.
    """
    def __init__(self, delta, dt= 1/80, normalization=None):
        super(Andrea, self).__init__(name='Andrea')
        self.delta = delta
        self.dt = dt
        self.normalization = normalization
        if normalization is not None:
            self.normalization = [np.array(norm, dtype=config.np_dtype)
                                  for norm in normalization]
            self.inv_norm = [norm ** -1 for norm in self.normalization]


    def normalize(self, state):
        """Normalize states."""
        if self.normalization is None:
            return state
        Tx_inv = np.diag(self.inv_norm)
        state = torch.mm(state, torch.tensor(Tx_inv, device = config.device))
        return state


    def denormalize(self, state):
        """De-normalize states."""
        if self.normalization is None:
            return state
        Tx = np.diag(self.normalization)
        state = torch.mm(state, torch.tensor(Tx, device = config.device))
        return state


    @concatenate_inputs(start=1)
    def eval(self, state):
        """Evaluate the dynamics.
    
        Parameters
        ----------
        state: ndarray or Tensor
            normalized states of the system.

        Returns
        -------
        normalized next state: Tensor
            The normalized next state after applying the dynamics for one timestep.
            
        """
        # Denormalize
        state = self.denormalize(state)
        n_inner = 10
        dt = self.dt / n_inner
        for i in range(n_inner):
            state_derivative = self.ode(state)
            state = state + dt * state_derivative
        return self.normalize(state)


    def ode(self, state):
        """Compute the state time-derivative.

        Parameters
        ----------
        states: ndarray or Tensor
            Unnormalized states.

        Returns
        -------
        x_dot: Tensor
            The normalized derivative of the dynamics

        """

        x1, x2 = torch.split(state, [1, 1], dim=1)
        x1dot = -x2 - 3/2 * x1**2 - 1/2 * x1**3 + self.delta
        x2dot = 3*x1 - x2 - x2**2
        state_derivative = torch.cat((x1dot, x2dot), dim=1)

        # Normalize
        return state_derivative

    def linearize(self):
        raise NotImplementedError
        

class DuffingOscillator(DeterministicFunction):
    """
    Parameters
    --------------
    mass(float): mass
    k_linear(float): linear stiffness 
    k_nonlinear(float): nonlinear stiffness
    damping(float) damping coefficient
    """

    def __init__(self, mass, k_linear, k_nonlinear, damping, dt= 1/80, normalization=None):
        super(DuffingOscillator, self).__init__()
        self.mass = mass
        self.k_linear = k_linear
        self.k_nonlinear = k_nonlinear
        self.damping = damping
        self.dt = dt
        self.normalization = normalization
        if normalization is not None:
            self.normalization = [np.array(norm, dtype=config.np_dtype)
                                  for norm in normalization]
            self.inv_norm = [norm ** -1 for norm in self.normalization]

    def normalize(self, state, action):
        """Normalize states and actions."""
        if self.normalization is None:
            return state, action

        Tx_inv, Tu_inv = map(np.diag, self.inv_norm)
        state = torch.mm(state, torch.tensor(Tx_inv, device = config.device))

        if action is not None:
            action = torch.mm(action, torch.tensor(Tu_inv, device = config.device))

        return state, action

    def denormalize(self, state, action):
        """De-normalize states and actions."""
        if self.normalization is None:
            return state, action

        Tx, Tu = map(np.diag, self.normalization)
        state = torch.mm(state, torch.tensor(Tx, device = config.device))
        if action is not None:
            action = torch.mm(action, torch.tensor(Tu, device = config.device))

        return state, action

    @concatenate_inputs(start=1)
    def eval(self, state_action):
        """Evaluate the dynamics."""
        # Denormalize
        state, action = torch.split(state_action, [2, 1], dim=1)
        state, action = self.denormalize(state, action)

        n_inner = 10
        dt = self.dt / n_inner
        for i in range(n_inner):
            state_derivative = self.ode(state, action)
            state = state + dt * state_derivative

        return self.normalize(state, None)[0]


    def ode(self, state, action):
        """Compute the state time-derivative.

        Parameters
        ----------
        states: ndarray or Tensor
            Unnormalized states.
        actions: ndarray or Tensor
            Unnormalized actions.

        Returns
        -------
        x_dot: Tensor
            The normalized derivative of the dynamics

        """

        position, velocity = torch.split(state, [1, 1], dim=1)
        x_ddot = 1 / self.mass * (- self.damping * velocity - self.k_linear * position - self.k_nonlinear *  position.pow(3) + action)
        state_derivative = torch.cat((velocity, x_ddot), dim=1)

        # Normalize
        return state_derivative

    def linearize(self):
        """Return the linearized system.

        Returns
        -------
        A : ndarray
            The state matrix.
        B : ndarray
            The action matrix.

        """
        A = np.array([[0, 1],
                        [-1 /self.mass * self.damping + -1 /self.mass * 3 * self.k_nonlinear, -self.damping / self.mass]])

        B = np.array([[0],
                    [-1/self.mass]])

        if self.normalization is not None:
            Tx, Tu = map(np.diag, self.normalization)
            Tx_inv, Tu_inv = map(np.diag, self.inv_norm)

        A = np.linalg.multi_dot((Tx_inv, A, Tx))
        B = np.linalg.multi_dot((Tx_inv, B, Tu))

        sys = signal.StateSpace(A, B, np.eye(2), np.zeros((2, 1)))
        sysd = sys.to_discrete(self.dt)
        return sysd.A, sysd.B


class InvertedPendulum(DeterministicFunction):
    """Inverted Pendulum.

    Parameters
    ----------
    mass : float
    length : float
    friction : float, optional
    dt : float, optional
        The sampling time.
    normalization : tuple, optional
        A tuple (Tx, Tu) of arrays used to normalize the state and actions. It
        is so that diag(Tx) *x_norm = x and diag(Tu) * u_norm = u.

    """

    def __init__(self, mass, length, friction=0, dt=1 / 80,
                 normalization=None):
        """Initialization; see `InvertedPendulum`."""
        super(InvertedPendulum, self).__init__()
        self.mass = mass
        self.length = length
        self.gravity = 9.81
        self.friction = friction
        self.dt = dt

        self.normalization = normalization
        if normalization is not None:
            # Upper and lower limits
            self.normalization = [np.array(norm, dtype=config.np_dtype)
                                  for norm in normalization]
            self.inv_norm = [norm ** -1 for norm in self.normalization]

    @property
    def inertia(self):
        """Return inertia of the pendulum."""
        return self.mass * self.length ** 2

    def normalize(self, state, action):
        """Normalize states and actions."""
        if self.normalization is None:
            return state, action

        Tx_inv, Tu_inv = map(np.diag, self.inv_norm)
        state = torch.mm(state, torch.tensor(Tx_inv, device = config.device))

        if action is not None:
            action = torch.mm(action, torch.tensor(Tu_inv, device = config.device))

        return state, action

    def denormalize(self, state, action):
        """De-normalize states and actions."""
        if self.normalization is None:
            return state, action

        Tx, Tu = map(np.diag, self.normalization)
        state = torch.mm(state, torch.tensor(Tx, device = config.device))
        if action is not None:
            action = torch.mm(action, torch.tensor(Tu, device = config.device))

        return state, action

    def linearize(self):
        """Return the linearized system.

        Returns
        -------
        a : ndarray
            The state matrix.
        b : ndarray
            The action matrix.

        """
        gravity = self.gravity
        length = self.length
        friction = self.friction
        inertia = self.inertia

        A = np.array([[0, 1],
                      [gravity / length, -friction / inertia]],
                     dtype=config.np_dtype)

        B = np.array([[0],
                      [1 / inertia]],
                     dtype=config.np_dtype)

        if self.normalization is not None:
            Tx, Tu = map(np.diag, self.normalization)
            Tx_inv, Tu_inv = map(np.diag, self.inv_norm)

            A = np.linalg.multi_dot((Tx_inv, A, Tx))
            B = np.linalg.multi_dot((Tx_inv, B, Tu))

        sys = signal.StateSpace(A, B, np.eye(2), np.zeros((2, 1)))
        sysd = sys.to_discrete(self.dt)
        return sysd.A, sysd.B

    def linearize_ct(self):
        """Return the linearized system.

        Returns
        -------
        a : ndarray
            The state matrix.
        b : ndarray
            The action matrix.

        """
        gravity = self.gravity
        length = self.length
        friction = self.friction
        inertia = self.inertia

        A = np.array([[0, 1],
                      [gravity / length, -friction / inertia]],
                     dtype=config.np_dtype)

        B = np.array([[0],
                      [1 / inertia]],
                     dtype=config.np_dtype)

        if self.normalization is not None:
            Tx, Tu = map(np.diag, self.normalization)
            Tx_inv, Tu_inv = map(np.diag, self.inv_norm)

            A = np.linalg.multi_dot((Tx_inv, A, Tx))
            B = np.linalg.multi_dot((Tx_inv, B, Tu))

        return A, B

    @concatenate_inputs(start=1)
    def eval(self, state_action):
        """Evaluate the dynamics."""
        # Denormalize
        state, action = torch.split(state_action, [2, 1], dim=1)
        state, action = self.denormalize(state, action)

        n_inner = 10
        dt = self.dt / n_inner
        for i in range(n_inner):
            state_derivative = self.ode(state, action)
            state = state + dt * state_derivative

        return self.normalize(state, None)[0]

    def ode(self, state, action):
        """Compute the state time-derivative.

        Parameters
        ----------
        states: ndarray or Tensor
            Unnormalized states.
        actions: ndarray or Tensor
            Unnormalized actions.

        Returns
        -------
        x_dot: Tensor
            The normalized derivative of the dynamics

        """
        # Physical dynamics
        gravity = self.gravity
        length = self.length
        friction = self.friction
        inertia = self.inertia

        angle, angular_velocity = torch.split(state, [1, 1], dim=1)

        x_ddot = gravity / length * torch.sin(angle) + action / inertia

        if friction > 0:
            x_ddot -= friction / inertia * angular_velocity

        state_derivative = torch.cat((angular_velocity, x_ddot), dim=1)

        return state_derivative

    def ode_normalized(self, state, action):
        """Compute the state time-derivative.

        Parameters
        ----------
        states: ndarray or Tensor
            Unnormalized states.
        actions: ndarray or Tensor
            Unnormalized actions.

        Returns
        -------
        x_dot: Tensor
            The normalized derivative of the dynamics

        """
        # Physical dynamics
        gravity = self.gravity
        length = self.length
        friction = self.friction
        inertia = self.inertia

        state, action = self.denormalize(state, action)
        angle, angular_velocity = torch.split(state, [1, 1], dim=1)
        x_ddot = gravity / length * torch.sin(angle) + action / inertia
        if friction > 0:
            x_ddot -= friction / inertia * angular_velocity
        state_derivative = torch.cat((angular_velocity, x_ddot), dim=1)

        # Normalize
        return self.normalize(state_derivative, None)[0]


class Backstepping_3D(DeterministicFunction):
    """Inverted Pendulum.

    Parameters
    ----------
    mass : float
    length : float
    friction : float, optional
    dt : float, optional
        The sampling time.
    normalization : tuple, optional
        A tuple (Tx, Tu) of arrays used to normalize the state and actions. It
        is so that diag(Tx) *x_norm = x and diag(Tu) * u_norm = u.

    """

    def __init__(self, a, b, c, d, dt=0.01, normalization=None):
        super(Backstepping_3D, self).__init__()
        self.a = a
        self.b = b
        self.c = c
        self.d = d
        self.dt = dt

        self.normalization = normalization
        if normalization is not None:
            # Upper and lower limits
            self.normalization = [np.array(norm, dtype=config.np_dtype)
                                  for norm in normalization]
            self.inv_norm = [norm ** -1 for norm in self.normalization]


    def normalize(self, state, action):
        """Normalize states and actions."""
        if self.normalization is None:
            return state, action

        Tx_inv, Tu_inv = map(np.diag, self.inv_norm)
        state = torch.mm(state, torch.tensor(Tx_inv, device = config.device))

        if action is not None:
            action = torch.mm(action, torch.tensor(Tu_inv, device = config.device))

        return state, action

    def denormalize(self, state, action):
        """De-normalize states and actions."""
        if self.normalization is None:
            return state, action

        Tx, Tu = map(np.diag, self.normalization)
        state = torch.mm(state, torch.tensor(Tx, device = config.device))
        if action is not None:
            action = torch.mm(action, torch.tensor(Tu, device = config.device))

        return state, action

    def linearize(self):
        """Return the linearized system.

        """
        a = self.a
        b = self.b
        c = self.c
        d = self.d

        A = A = np.array([[0, a, 0],[0, 0, b],[2*c, 0, 0]], dtype = config.np_dtype)
        B = np.array([[0],[0],[d]], dtype = config.np_dtype)


        if self.normalization is not None:
            Tx, Tu = map(np.diag, self.normalization)
            Tx_inv, Tu_inv = map(np.diag, self.inv_norm)

            A = np.linalg.multi_dot((Tx_inv, A, Tx))
            B = np.linalg.multi_dot((Tx_inv, B, Tu))

        sys = signal.StateSpace(A, B, np.eye(2), np.zeros((2, 1)))
        sysd = sys.to_discrete(self.dt)
        return sysd.A, sysd.B

    def linearize_ct(self):
        """Return the linearized system.

        Returns
        -------
        a : ndarray
            The state matrix.
        b : ndarray
            The action matrix.

        """
        a = self.a
        b = self.b
        c = self.c
        d = self.d

        A = A = np.array([[0, a, 0],[0, 0, b],[2*c, 0, 0]], dtype = config.np_dtype)
        B = np.array([[0],[0],[d]], dtype = config.np_dtype)

        if self.normalization is not None:
            Tx, Tu = map(np.diag, self.normalization)
            Tx_inv, Tu_inv = map(np.diag, self.inv_norm)

            A = np.linalg.multi_dot((Tx_inv, A, Tx))
            B = np.linalg.multi_dot((Tx_inv, B, Tu))

        return A, B

    @concatenate_inputs(start=1)
    def eval(self, state_action):
        """Evaluate the dynamics."""
        # Denormalize
        state, action = torch.split(state_action, [3, 1], dim=1)
        state, action = self.denormalize(state, action)

        n_inner = 10
        dt = self.dt / n_inner
        for i in range(n_inner):
            state_derivative = self.ode(state, action)
            state = state + dt * state_derivative

        return self.normalize(state, None)[0]

    def ode(self, state, action):
        """Compute the state time-derivative.

        Parameters
        ----------
        states: ndarray or Tensor
            Unnormalized states.
        actions: ndarray or Tensor
            Unnormalized actions.

        Returns
        -------
        x_dot: Tensor
            The normalized derivative of the dynamics

        """
        # Physical dynamics
        a = self.a
        b = self.b
        c = self.c
        d = self.d

        x1, x2, x3 = torch.split(state, [1, 1, 1], dim=1)
        x1_dot = a * x2
        x2_dot = b * x3
        x3_dot = c * torch.pow(x1, 2) + d * action

        state_derivative = torch.cat((x1_dot, x2_dot, x3_dot), dim=1)

        return state_derivative

    def ode_normalized(self, state, action):
        """Compute the state time-derivative.

        Parameters
        ----------
        states: ndarray or Tensor
            Unnormalized states.
        actions: ndarray or Tensor
            Unnormalized actions.

        Returns
        -------
        x_dot: Tensor
            The normalized derivative of the dynamics

        """
        # Physical dynamics
        a = self.a
        b = self.b
        c = self.c
        d = self.d

        state, action = self.denormalize(state, action)
        x1, x2, x3 = torch.split(state, [1, 1, 1], dim=1)
        x1_dot = a * x2
        x2_dot = b * x3
        x3_dot = c * torch.pow(x1, 2) + d * action
        state_derivative = torch.cat((x1_dot, x2_dot, x3_dot), dim=1)

        # Normalize
        return self.normalize(state_derivative, None)[0]


class CartPole(DeterministicFunction):
    """
    Parameters
    ----------
    pendulum_mass : float
    cart_mass : float
    length : float
    dt : float, optional
        The sampling period used for discretization.
    normalization : tuple, optional
        A tuple (Tx, Tu) of 1-D arrays or lists used to normalize the state and
        action, such that x = diag(Tx) * x_norm and u = diag(Tu) * u_norm.

    """
    
    def __init__(self, pendulum_mass, cart_mass, length, friction=0.0, 
                dt=0.01, normalization=None):
        """Initialization; see `CartPole`.""" 
        super(CartPole, self).__init__()
        self.pendulum_mass = pendulum_mass
        self.cart_mass = cart_mass
        self.length = length
        self.friction = friction
        self.dt = dt
        self.gravity = 9.81
        self.state_dim = 4
        self.action_dim = 1
        self.normalization = normalization
        if normalization is not None:
            self.normalization = [np.array(norm, dtype=config.np_dtype)
                                  for norm in normalization]
            self.inv_norm = [norm ** -1 for norm in self.normalization]

    def normalize(self, state, action):
        """Normalize states and actions."""
        if self.normalization is None:
            return state, action

        Tx_inv, Tu_inv = map(np.diag, self.inv_norm)
        state = torch.mm(state, torch.tensor(Tx_inv, device = config.device))

        if action is not None:
            action = torch.mm(action, torch.tensor(Tu_inv, device = config.device))
        
        return state, action

    def denormalize(self, state, action):
        """De-normalize states and actions."""
        if self.normalization is None:
            return state, action

        Tx, Tu = map(np.diag, self.normalization)
        state = torch.mm(state, torch.tensor(Tx, device = config.device))
        if action is not None:
            action = torch.mm(action, torch.tensor(Tu, device = config.device))

        return state, action

    def linearize_ct(self):
        """Return the discretized, scaled, linearized system.

        Returns
        -------
        Ad : ndarray
            The discrete-time state matrix.
        Bd : ndarray
            The discrete-time action matrix.

        """
        m = self.pendulum_mass
        M = self.cart_mass
        L = self.length
        b = self.friction
        g = self.gravity

        A = np.array([[0, 0,                   1, 0                            ],
                    [0, 0,                     0, 1                            ],
                    [0, m*g/M,              -b/M, 0                            ],
                    [0, g * (m + M) / (L * M), -b/(M*L), 0                     ]],
                    dtype=config.np_dtype)

        B = np.array([0, 0, 1/M, 1 / (M * L)]).reshape((-1, self.action_dim))

        if self.normalization is not None:
            Tx, Tu = map(np.diag, self.normalization)
            Tx_inv, Tu_inv = map(np.diag, self.inv_norm)
            A = np.linalg.multi_dot((Tx_inv, A, Tx))
            B = np.linalg.multi_dot((Tx_inv, B, Tu))

        return A, B

    def ode(self, state, action):
        """Compute the state time-derivative.

        Parameters
        ----------
        state: ndarray or Tensor
            States.
        action: ndarray or Tensor
            Actions.

        Returns
        -------
        state_derivative: Tensor
            The state derivative according to the dynamics.

        """
        # Physical dynamics
        m = self.pendulum_mass
        M = self.cart_mass
        L = self.length
        b = self.friction
        g = self.gravity

        x, theta, v, omega = torch.split(state, [1, 1, 1, 1], dim=1)

        x_dot = v
        theta_dot = omega

        det = M + m * torch.mul(torch.sin(theta), torch.sin(theta))
        v_dot = (action - b * v - m * L * torch.mul(omega, omega) * torch.sin(theta)  + 0.5 * m * g * torch.sin(2 * theta)) / det
        omega_dot = (action * torch.cos(theta) - 0.5 * m * L * torch.mul(omega, omega) * torch.sin(2 * theta) - b * torch.mul(v, torch.cos(theta))
                    + (m + M) * g * torch.sin(theta)) / (det * L)

        state_derivative = torch.cat((x_dot, theta_dot, v_dot, omega_dot), dim=1)

        return state_derivative
    
    def ode_normalized(self, state, action):
        """Compute the state time-derivative.

        Parameters
        ----------
        states: ndarray or Tensor
            Unnormalized states.
        actions: ndarray or Tensor
            Unnormalized actions.

        Returns
        -------
        x_dot: Tensor
            The normalized derivative of the dynamics

        """
        # Physical dynamics
        m = self.pendulum_mass
        M = self.cart_mass
        L = self.length
        b = self.friction
        g = self.gravity

        state, action = self.denormalize(state, action)
        x, theta, v, omega = torch.split(state, [1, 1, 1, 1], dim=1)

        x_dot = v
        theta_dot = omega

        det = M + m * torch.mul(torch.sin(theta), torch.sin(theta))
        v_dot = (action - b * v - m * L * torch.mul(omega, omega) * torch.sin(theta)  + 0.5 * m * g * torch.sin(2 * theta)) / det
        omega_dot = (action * torch.cos(theta) - 0.5 * m * L * torch.mul(omega, omega) * torch.sin(2 * theta) - b * torch.mul(v, torch.cos(theta))
                    + (m + M) * g * torch.sin(theta)) / (det * L)

        state_derivative = torch.cat((x_dot, theta_dot, v_dot, omega_dot), dim=1)

        # Normalize
        return self.normalize(state_derivative, None)[0]


class Euler_3D(DeterministicFunction):
    """Inverted Pendulum.

    Parameters
    ----------
    mass : float
    length : float
    friction : float, optional
    dt : float, optional
        The sampling time.
    normalization : tuple, optional
        A tuple (Tx, Tu) of arrays used to normalize the state and actions. It
        is so that diag(Tx) *x_norm = x and diag(Tu) * u_norm = u.

    """

    def __init__(self, J1, J2, J3, dt=0.01, normalization=None):
        super(Euler_3D, self).__init__()
        self.J1 = J1
        self.J2 = J2
        self.J3 = J3
        self.dt = dt

        self.normalization = normalization
        if normalization is not None:
            # Upper and lower limits
            self.normalization = [np.array(norm, dtype=config.np_dtype)
                                  for norm in normalization]
            self.inv_norm = [norm ** -1 for norm in self.normalization]


    def normalize(self, state, action):
        """Normalize states and actions."""
        if self.normalization is None:
            return state, action

        Tx_inv, Tu_inv = map(np.diag, self.inv_norm)
        state = torch.mm(state, torch.tensor(Tx_inv, device = config.device))

        if action is not None:
            action = torch.mm(action, torch.tensor(Tu_inv, device = config.device))

        return state, action

    def denormalize(self, state, action):
        """De-normalize states and actions."""
        if self.normalization is None:
            return state, action

        Tx, Tu = map(np.diag, self.normalization)
        state = torch.mm(state, torch.tensor(Tx, device = config.device))
        if action is not None:
            action = torch.mm(action, torch.tensor(Tu, device = config.device))

        return state, action

    def linearize(self):
        """Return the linearized system.

        """
        J1 = self.J1
        J2 = self.J2
        J3 = self.J3

        A = A = np.zeros((3,3), dtype = config.np_dtype)
        B = np.diag([1/J1, 1/J2, 1/J3]).astype(config.np_dtype)


        if self.normalization is not None:
            Tx, Tu = map(np.diag, self.normalization)
            Tx_inv, Tu_inv = map(np.diag, self.inv_norm)

            A = np.linalg.multi_dot((Tx_inv, A, Tx))
            B = np.linalg.multi_dot((Tx_inv, B, Tu))

        sys = signal.StateSpace(A, B, np.eye(2), np.zeros((2, 1)))
        sysd = sys.to_discrete(self.dt)
        return sysd.A, sysd.B

    def linearize_ct(self):
        """Return the linearized system.

        Returns
        -------
        a : ndarray
            The state matrix.
        b : ndarray
            The action matrix.

        """
        J1 = self.J1
        J2 = self.J2
        J3 = self.J3

        A = A = np.zeros((3,3), dtype = config.np_dtype)
        B = np.diag([1/J1, 1/J2, 1/J3]).astype(config.np_dtype)

        if self.normalization is not None:
            Tx, Tu = map(np.diag, self.normalization)
            Tx_inv, Tu_inv = map(np.diag, self.inv_norm)

            A = np.linalg.multi_dot((Tx_inv, A, Tx))
            B = np.linalg.multi_dot((Tx_inv, B, Tu))

        return A, B

    @concatenate_inputs(start=1)
    def eval(self, state_action):
        """Evaluate the dynamics."""
        # Denormalize
        state, action = torch.split(state_action, [3, 1], dim=1)
        state, action = self.denormalize(state, action)

        n_inner = 10
        dt = self.dt / n_inner
        for i in range(n_inner):
            state_derivative = self.ode(state, action)
            state = state + dt * state_derivative

        return self.normalize(state, None)[0]

    def ode(self, state, action):
        """Compute the state time-derivative.

        Parameters
        ----------
        states: ndarray or Tensor
            Unnormalized states.
        actions: ndarray or Tensor
            Unnormalized actions.

        Returns
        -------
        x_dot: Tensor
            The normalized derivative of the dynamics

        """
        # Physical dynamics
        J1 = self.J1
        J2 = self.J2
        J3 = self.J3

        x1, x2, x3 = torch.split(state, [1, 1, 1], dim=1)
        x1_dot =  (J2 - J3)/J1 * torch.mul(x2, x3)
        x2_dot =  (J3 - J1)/J2 * torch.mul(x3, x1)
        x3_dot =  (J1 - J2)/J3 * torch.mul(x1, x2)
        tmp = torch.tensor([1/J1, 1/J2, 1/J3], dtype = config.ptdtype)
        coef = torch.diag(tmp)
        state_derivative = torch.cat((x1_dot, x2_dot, x3_dot), dim=1) +\
             torch.matmul(action, coef)

        return state_derivative

    def ode_normalized(self, state, action):
        """Compute the state time-derivative.

        Parameters
        ----------
        states: ndarray or Tensor
            Unnormalized states.
        actions: ndarray or Tensor
            Unnormalized actions.

        Returns
        -------
        x_dot: Tensor
            The normalized derivative of the dynamics

        """
        # Physical dynamics
        J1 = self.J1
        J2 = self.J2
        J3 = self.J3

        state, action = self.denormalize(state, action)
        x1, x2, x3 = torch.split(state, [1, 1, 1], dim=1)
        x1_dot =  (J2 - J3)/J1 * torch.mul(x2, x3)
        x2_dot =  (J3 - J1)/J2 * torch.mul(x3, x1)
        x3_dot =  (J1 - J2)/J3 * torch.mul(x1, x2)
        tmp = torch.tensor([1/J1, 1/J2, 1/J3], dtype = config.ptdtype)
        coef = torch.diag(tmp)
        state_derivative = torch.cat((x1_dot, x2_dot, x3_dot), dim=1) +\
             torch.matmul(action, coef)

        # Normalize
        return self.normalize(state_derivative, None)[0]


class VanDerPol(DeterministicFunction):
    """Van der Pol oscillator in reverse-time."""

    def __init__(self, damping=1, dt=0.01, normalization=None):
        """Initialization; see `VanDerPol`."""
        super(VanDerPol, self).__init__(name='VanDerPol')
        self.damping = damping
        self.dt = dt
        self.state_dim = 2
        self.action_dim = 0
        self.normalization = normalization
        if normalization is not None:
            self.normalization = np.array(normalization, dtype=config.np_dtype)
            self.inv_norm = self.normalization ** -1

    def normalize(self, state):
        """Normalize states."""
        if self.normalization is None:
            return state
        Tx_inv = np.diag(self.inv_norm)
        state = torch.mm(state, torch.tensor(Tx_inv, device = config.device))
        return state

    def denormalize(self, state):
        """De-normalize states and actions."""
        if self.normalization is None:
            return state
        Tx = np.diag(self.normalization)
        state = torch.mm(state, torch.tensor(Tx, device = config.device))
        return state

    def linearize(self):
        """Return the discretized, scaled, linearized system.

        Returns
        -------
        Ad : ndarray
            The discrete-time state matrix.

        """
        A = np.array([[0, -1], [1, -1]], dtype=config.np_dtype)
        B = np.zeros([2, 1])
        if self.normalization is not None:
            Tx = np.diag(self.normalization)
            Tx_inv = np.diag(self.inv_norm)
            A = np.linalg.multi_dot((Tx_inv, A, Tx))
        B = np.zeros([2, 1])

        Ad, _, _, _, _ = signal.cont2discrete((A, B, 0, 0), self.dt, method='zoh')

        return Ad

    @concatenate_inputs(start=1)
    def eval(self, state):
        """Evaluate the dynamics.
        
        Parameters
        ----------
        state: ndarray or Tensor
            normalized states of the system.

        Returns
        -------
        normalized next state: Tensor
            The normalized next state after applying the dynamics for one timestep.
            
        """
        # Denormalize
        state = self.denormalize(state)
        n_inner = 10
        dt = self.dt / n_inner
        for i in range(n_inner):
            state_derivative = self.ode(state)
            state = state + dt * state_derivative
        return self.normalize(state)

    def ode(self, state):
        """Compute the state time-derivative.

        Parameters
        ----------
        state: ndarray or Tensor
            States.

        Returns
        -------
        state_derivative: Tensor
            The state derivative according to the dynamics.

        """
        # Physical dynamics
        damping = self.damping
        x, y = torch.split(state, [1, 1], dim=1)
        x_dot = - y
        y_dot = x + damping * (x ** 2 - 1) * y
        state_derivative = torch.cat((x_dot, y_dot), dim=1)
        return state_derivative


def compute_roa(grid, closed_loop_dynamics, horizon=100, tol=1e-3, equilibrium=None, no_traj=True):
    """Compute the largest ROA as a set of states in a discretization.
    
    Parameters
    ----------
    grid: ndarray or a GridWorld instance
        The set of initial states to check for stability.
    closed_loop_dynamics: PT function
        Takes the current state and produces the next state.
    horizon: int
        How far the simulation of each state should go to check for stability (The longer, more accurate but more costly).
    tol: float,
        How large the gap between the final state and the origin can be (The larger, more states are considered as stable).
    equilibrium: ndarray
        The equilibrium wrt which the final state of the simulated trajetcories are compared.
    no_traj: Boolean
        If False, the simulated trajectories are kept and returned.

    Returns
    -------
    roa: ndarray
        Binary array where the points beloning to roa are labeled True and the rest are labeled False.
    trajectories: ndarray
        If no_traj is false, the simulated trajectories for all initial points of the provided grid are returned as a ndarray.
    
    """


    if isinstance(grid, np.ndarray):
        all_points = grid
        nindex = grid.shape[0]
        ndim = grid.shape[1]
    else: # grid is a GridWorld instance
        all_points = grid.all_points
        nindex = grid.nindex
        ndim = grid.ndim

    # Forward-simulate all trajectories from initial points in the discretization
    if no_traj:
        end_states = all_points
        for t in range(1, horizon):
            end_states = closed_loop_dynamics(end_states)
    else:
        trajectories = np.empty((nindex, ndim, horizon))
        trajectories[:, :, 0] = all_points
        with torch.no_grad():
            for t in range(1, horizon):
                trajectories[:, :, t] = closed_loop_dynamics(trajectories[:, :, t - 1])
        end_states = trajectories[:, :, -1]

    if equilibrium is None:
        equilibrium = np.zeros((1, ndim))

    # Compute an approximate ROA as all states that end up "close" to 0
    dists = np.linalg.norm(end_states - equilibrium, ord=2, axis=1, keepdims=True).ravel()
    roa = (dists <= tol)
    if no_traj:
        return roa
    else:
        return roa, trajectories

def compute_roa_ct(grid, closed_loop_dynamics, dt, horizon=100, tol=1e-3, equilibrium=None, no_traj=True):
    if isinstance(grid, np.ndarray):
        all_points = grid
        nindex = grid.shape[0]
        ndim = grid.shape[1]
    else: # grid is a GridWorld instance
        all_points = grid.all_points
        nindex = grid.nindex
        ndim = grid.ndim

    # Forward-simulate all trajectories from initial points in the discretization
    horizon = horizon
    dt = dt
    if no_traj:
        end_states = all_points
        with torch.no_grad():
            for t in range(1, horizon):
                end_states = closed_loop_dynamics(end_states).detach().cpu().numpy()*dt + end_states
    else:
        trajectories = np.empty((nindex, ndim, horizon))
        trajectories[:, :, 0] = all_points
        with torch.no_grad():
            for t in range(1, horizon):
                trajectories[:, :, t] = closed_loop_dynamics(trajectories[:, :, t - 1]).cpu().numpy()*dt + trajectories[:, :, t - 1]
        end_states = trajectories[:, :, -1]

    if equilibrium is None:
        equilibrium = np.zeros((1, ndim))

    # Compute an approximate ROA as all states that end up "close" to 0
    dists = np.linalg.norm(end_states - equilibrium, ord=2, axis=1, keepdims=True).ravel()
    roa = (dists <= tol)
    if no_traj:
        return roa
    else:
        return roa, trajectories

def compute_roa_zero_v(grid, closed_loop_dynamics, horizon=100, tol=1e-3, equilibrium=None, no_traj=True):
    """Computes those states in the discritization grid that evolved to some stationary point (no necessarily the origin).
    
    Parameters
    ----------
    grid: ndarray or a GridWorld instance
        The set of initial states to check for stability.
    closed_loop_dynamics: PT function
        Takes the current state and produces the next state.
    horizon: int
        How far the simulation of each state should go to check for stability (The longer, more accurate but more constly).
    tol: float,
        How large the gap between the final state and the origin can be (The larger, more states are considered as stable).
    equilibrium: ndarray
        The equilibrium wrt which the final state of the simulated trajetcories are compared.
    no_traj: Boolean
        If False, the simulated trajectories are kept and returned.

    Returns
    -------
    roa: ndarray
        Binary array where the points beloning to roa are labeled True and the rest are labeled False.
    trajectories: ndarray
        If no_traj is false, the simulated trajectories for all initial points of the provided grid are returned as a ndarray.
    
    """
    if isinstance(grid, np.ndarray):
        all_points = grid
        nindex = grid.shape[0]
        ndim = grid.shape[1]
    else: # grid is a GridWorld instance
        all_points = grid.all_points
        nindex = grid.nindex
        ndim = grid.ndim

    # Forward-simulate all trajectories from initial points in the discretization
    if no_traj:
        end_states_one_before = all_points
        for t in range(1, horizon-1):
            end_states_one_before = closed_loop_dynamics(end_states_one_before)
        end_states = closed_loop_dynamics(end_states_one_before)
    else:
        trajectories = np.empty((nindex, ndim, horizon))
        trajectories[:, :, 0] = all_points
        with torch.no_grad():
            for t in range(1, horizon):
                trajectories[:, :, t] = closed_loop_dynamics(trajectories[:, :, t - 1])
        end_states_one_before = trajectories[:, :, -2]
        end_states = trajectories[:, :, -1]

    # Compute an approximate ROA as all states that end up "close" to 0
    dists = np.linalg.norm(end_states - end_states_one_before, ord=2, axis=1, keepdims=True).ravel()
    roa = (dists <= tol)
    if no_traj:
        return roa
    else:
        return roa, trajectories

def monomials(x, deg):
    """Compute monomial features of `x' up to degree `deg'."""
    x = np.atleast_2d(np.copy(x))
    # 1-D features (x, y)
    Z = x
    if deg >= 2:
        # 2-D features (x^2, x * y, y^2)
        temp = np.empty([len(x), 3])
        temp[:, 0] = x[:, 0] ** 2
        temp[:, 1] = x[:, 0] * x[:, 1]
        temp[:, 2] = x[:, 1] ** 2
        Z = np.hstack((Z, temp))
    if deg >= 3:
        # 3-D features (x^3, x^2 * y, x * y^2, y^3)
        temp = np.empty([len(x), 4])
        temp[:, 0] = x[:, 0] ** 3
        temp[:, 1] = (x[:, 0] ** 2) * x[:, 1]
        temp[:, 2] = x[:, 0] * (x[:, 1] ** 2)
        temp[:, 3] = x[:, 1] ** 3
        Z = np.hstack((Z, temp))
    if deg >= 4:
        # 4-D features (x^4, x^3 * y, x^2 * y^2, x * y^3, y^4)
        temp = np.empty([len(x), 5])
        temp[:, 0] = x[:, 0] ** 4
        temp[:, 1] = (x[:, 0] ** 3) * x[:, 1]
        temp[:, 2] = (x[:, 0] ** 2) * (x[:, 1] ** 2)
        temp[:, 3] = x[:, 0] * (x[:, 1] ** 3)
        temp[:, 4] = x[:, 1] ** 4
        Z = np.hstack((Z, temp))
    return Z

def derivative_monomials(x, deg):
    """Compute derivatives of monomial features of `x' up to degree `deg'."""
    x = np.atleast_2d(np.copy(x))
    dim = x.shape[1]
    # 1-D features (x, y)
    Z = np.zeros([len(x), 2, dim])
    Z[:, 0, 0] = 1
    Z[:, 1, 1] = 1
    if deg >= 2:
        # 2-D features (x^2, x * y, y^2)
        temp = np.zeros([len(x), 3, dim])
        temp[:, 0, 0] = 2 * x[:, 0]
        temp[:, 1, 0] = x[:, 1]
        temp[:, 1, 1] = x[:, 0]
        temp[:, 2, 1] = 2 * x[:, 1]
        Z = np.concatenate((Z, temp), axis=1)
    if deg >= 3:
        # 3-D features (x^3, x^2 * y, x * y^2, y^3)
        temp = np.zeros([len(x), 4, dim])
        temp[:, 0, 0] = 3 * x[:, 0] ** 2
        temp[:, 1, 0] = 2 * x[:, 0] * x[:, 1]
        temp[:, 1, 1] = x[:, 0] ** 2
        temp[:, 2, 0] = x[:, 1] ** 2
        temp[:, 2, 1] = 2 * x[:, 0] * x[:, 1]
        temp[:, 3, 1] = 3 * x[:, 1] ** 2
        Z = np.concatenate((Z, temp), axis=1)
    return Z

def binary_cmap(color='red', alpha=1.):
    """Construct a binary colormap."""
    if color == 'red':
        color_code = (1., 0., 0., alpha)
    elif color == 'green':
        color_code = (0., 1., 0., alpha)
    elif color == 'blue':
        color_code = (0., 0., 1., alpha)
    else:
        color_code = color
    transparent_code = (1., 1., 1., 0.)
    return ListedColormap([transparent_code, color_code])

def balanced_class_weights(y_true, scale_by_total=True):
    """Compute class weights from class label counts."""
    y = y_true.astype(np.bool)
    nP = y.sum()
    nN = y.size - y.sum()
    class_counts = np.array([nN, nP])

    weights = np.ones_like(y, dtype=float)
    weights[ y] /= nP
    weights[~y] /= nN
    if scale_by_total:
        weights *= y.size

    return weights, class_counts

def generate_trajectories(states_init, closed_loop_dynamics, dt, horizon):
    if isinstance(states_init, np.ndarray):
        states_init = torch.tensor(np.copy(states_init), dtype=config.ptdtype, device = config.device)
    nindex = states_init.shape[0]
    ndim = states_init.shape[1]
    
    trajectories = torch.zeros((nindex, ndim, horizon+1), dtype=config.ptdtype, device = config.device)
    grad_field = torch.zeros((nindex, ndim, horizon), dtype=config.ptdtype, device = config.device)
    trajectories[:, :, 0] = states_init
    
    with torch.no_grad():
        for t in range(1, horizon+1):
            trajectories[:, :, t] = closed_loop_dynamics(trajectories[:, :, t - 1])
            grad_field[:, :, t-1] = (trajectories[:, :, t] - trajectories[:, :, t-1]) / dt
    return trajectories[:,:, 0:-1], grad_field

def build_system(system_properties, dt):
    """
    Takes an instance of system_property class and return a 
    system class based on the type of the system.
    """
    s = system_properties
    if s.type == "pendulum":
        system = InvertedPendulum(s.m , s.L, s.b, dt, [s.state_norm, s.action_norm])
    elif s.type == "backstepping_3d":
        system = Backstepping_3D(s.a, s.b, s.c, s.d, dt, [s.state_norm, s.action_norm])
    elif s.type == "cartpole":
        system = CartPole(s.m, s.M, s.l, s.b, dt, [s.state_norm, s.action_norm])
    elif s.type == "euler_equation_3d":
        system = Euler_3D(s.J1, s.J2, s.J3, dt, [s.state_norm, s.action_norm])
    else:
        raise ValueError("No matching for system type {}!".format(s.type))
    return system

def save_dict(dict_obj, fullname):
    with open(fullname, 'wb') as handle:
        pickle.dump(dict_obj, handle, protocol=pickle.HIGHEST_PROTOCOL)

def load_dict(fullname):
    with open(fullname, 'rb') as handle:
        loaded_obj = pickle.load(handle)
    return loaded_obj

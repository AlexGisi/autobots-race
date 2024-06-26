from typing import Type, List
import casadi as ca
from models.State import State
from models.VehicleParameters import VehicleParameters
from control.ControllerParameters import FixedControllerParameters, RuntimeControllerParameters
from control.util import make_poly, deg2rad, normal_pdf


class MPC:
    def __init__(self,
                 state: Type[State],
                 s0: float, 
                 centerline_x_poly_coeffs: List[float],
                 centerline_y_poly_coeffs: List[float],
                 max_error: float,
                 runtime_params: Type[RuntimeControllerParameters],
                 sol0: None  # return of previous iteration
                 ) -> None:
        """
        state: initial vehicle state
        s: initial vehicle progress
        centerline_{x,y}_poly_coeffs: coefficients of centerline polynomial (over ControllerParameters.lookahead_distance)
        params: runtime parameters for this instantiation of the problem
        sol0: last solution returned by opti.solve(), optional
        """
        opti = ca.Opti()

        self.fixed_params = FixedControllerParameters()
        self.runtime_params = runtime_params
        self.state0 = state

        # Unpack variables for clarity.
        N = self.fixed_params.N
        n = self.runtime_params.n
        q_v_y = self.runtime_params.q_v_y
        alpha_c = self.runtime_params.alpha_c
        alpha_L = self.fixed_params.alpha_L
        beta_delta = self.runtime_params.beta_delta
        q_v_max = self.fixed_params.q_v_max
        v_max = self.fixed_params.v_max
        lambda_s = self.fixed_params.lambda_s

        min_steer = -1.0
        max_steer = 1.0
        min_throttle = -1.0
        max_throttle = 1.0
        max_steer_delta = 0.1
        max_throttle_delta = 0.2
        min_s_delta = 0.1
        max_s_delta = VehicleParameters.Ts * VehicleParameters.max_vel  # max progress per timestep

        # Decision variables. Column i is the <u/s/x> vector at time i. 
        U = opti.variable(2, N)  # throttle, steer in [-1, 1]
        S_hat = opti.variable(1, N+1)  # estimated progress (free)
        States = opti.variable(6, N+1) # X, Y, yaw, vx, vy, r

        # Symbols.
        s = ca.SX.sym('s') # Progress
        X = ca.SX.sym('X', 6, 1)  # State
        u = ca.SX.sym('u', 2, 1)  # Command

        centerline_x_poly = make_poly(s, centerline_x_poly_coeffs)
        centerline_y_poly = make_poly(s, centerline_y_poly_coeffs)

        Gx = ca.Function('Gx', [s], [centerline_x_poly])
        Gy = ca.Function('Gy', [s], [centerline_y_poly])
        dGx = ca.Function('dGx', [s], [ca.gradient(centerline_x_poly, s)])
        dGy = ca.Function('dGy', [s], [ca.gradient(centerline_y_poly, s)])

        e_hat_C = ca.Function('e_hat_C', [s, X], [dGy(s)*(X[0] - Gx(s)) - dGx(s)*(X[1] - Gy(s))])
        e_hat_L = ca.Function('e_hat_L', [s, X], [-dGx(s)*(X[0] - Gx(s)) - dGy(s)*(X[1] - Gy(s))])
        S = ca.Function('S', [s, X], [e_hat_C(s, X)**2 + e_hat_L(s, X)**2])

        f_vehicle = ca.Function('f_vehicle', [X, u], [self.f_vehicle(X, u)])
        
        # Cost function (terminal costs).
        J = -lambda_s*S(S_hat[N], States[:, N])
        J += q_v_y * States[4, N]**2 
        J += alpha_c*e_hat_C(S_hat[0, N], States[:, N])**n 
        J += alpha_L*e_hat_L(S_hat[0, N], States[:, N]) 
        J += ca.exp(q_v_max * (States[3, N] - v_max))

        # Cost function (stage costs). 
        for i in range(1, N):
            J += q_v_y*S(S_hat[i], States[:, i])**2
            J += alpha_c*e_hat_C(S_hat[i], States[:, i])**n
            J += alpha_L*e_hat_L(S_hat[i], States[:, i])
            J += beta_delta*(U[1, i]-U[1, i-1])**2
            J += ca.exp(q_v_max*(States[3, i] - v_max))

        # Initial conditions.
        opti.subject_to(S_hat[0] == s0)
        opti.subject_to(States[0, 0] == state.x)
        opti.subject_to(States[1, 0] == state.y)
        opti.subject_to(States[2, 0] == state.yaw)
        opti.subject_to(States[3, 0] == state.v_x)
        opti.subject_to(States[4, 0] == state.v_y)
        opti.subject_to(States[5, 0] == state.yaw_dot)

        # Constraints, convienent to do some basic initialization here for now (TODO: improve).
        state_direction = ca.vertcat([ca.cos(state.yaw), ca.sin(state.yaw)])
        state_direction = state_direction / ca.sqrt(state_direction[0]**2 + state_direction[1]**2)
        for i in range(1, N+1):
            if sol0 is None:
                opti.set_initial(S_hat[i], s0 + i*VehicleParameters.Ts*VehicleParameters.max_vel)
                opti.set_initial(States[0, i], state.x + state_direction[0]*i*VehicleParameters.Ts * (VehicleParameters.max_vel / 5))
                opti.set_initial(States[1, i], state.y + state_direction[1]*i*VehicleParameters.Ts * (VehicleParameters.max_vel / 5))
                opti.set_initial(States[2, i], state.yaw)
                opti.set_initial(States[3, i], state.v_x)
                opti.set_initial(States[4, i], state.v_y)
                opti.set_initial(States[5, i], state.yaw_dot)

            opti.subject_to(States[:, i] == f_vehicle(States[:, i-1], U[:, i-1]))
            opti.subject_to( opti.bounded(min_s_delta, S_hat[i] - S_hat[i-1], max_s_delta) )
            opti.subject_to( opti.bounded(-max_error-10, e_hat_C(S_hat[i], States[:, i]), max_error+10))
            # TODO EXAMINE MAX ERROR BOUNDS
            
        for i in range(0, N):
            opti.subject_to(U[0, i] < max_throttle)
            opti.subject_to(U[0, i] > min_throttle)
            opti.subject_to(U[1, i] < max_steer)
            opti.subject_to(U[1, i] > min_steer)
            opti.subject_to( opti.bounded(-max_throttle, U[0, i] - U[0, i-1], max_throttle_delta))
            opti.subject_to( opti.bounded(-max_steer_delta, U[1, i] - U[1, i-1], max_steer_delta))
        
        if sol0:  # TODO: dual variables
            opti.set_initial(sol0.value_variables())

        # Should be set regardless of sol0.
        opti.set_initial(S_hat[0], s0)
        opti.set_initial(States[0, 0], state.x)
        opti.set_initial(States[1, 0], state.y)
        opti.set_initial(States[2, 0], state.yaw)
        opti.set_initial(States[3, 0], state.v_x)
        opti.set_initial(States[4, 0], state.v_y)
        opti.set_initial(States[5, 0], state.yaw_dot)

        opti.minimize(J)
        opti.solver('ipopt', {
            'ipopt': {
                'max_iter': FixedControllerParameters.max_iter,  # Maximum number of iterations
                'print_level': 5,  # Adjust to control the verbosity of IPOPT output
                'tol': 1e-4  # Solver tolerance
            }
        })

        # Use same return structure so failures can be handled in the same way.
        try:
            self.sol = opti.solve()
            self.ret = (self.sol.value(States), 
                        self.sol.value(U), 
                        self.sol.value(S_hat), 
                        [self.sol.value(e_hat_C(S_hat[i], States[:, i])) for i in range(0, N)], 
                        [self.sol.value(e_hat_L(S_hat[i], States[:, i])) for i in range(N)])
        except RuntimeError as e:  # found infeasible solution or exceeded iterations or ...
            print(e)
            self.ret = (opti.debug.value(States), 
                        opti.debug.value(U), 
                        opti.debug.value(S_hat), 
                        [opti.debug.value(e_hat_C(S_hat[i], States[:, i])) for i in range(N)], 
                        [opti.debug.value(e_hat_L(S_hat[i], States[:, i])) for i in range(N)])
            self.sol = None
    
    def solution(self):
        return self.sol, self.ret

    def f_vehicle(self, x_k, u_k):
        """
        x_k: column of vehicle states
        u_k: column of commands (throttle, steer) in [-1, 1]
        Vehicle dynamics for state constraint.
        """
        # Unpack parameters.
        m = VehicleParameters.m
        Iz = VehicleParameters.Iz
        lf = VehicleParameters.lf
        lr = VehicleParameters.lr
        Cf = VehicleParameters.Cf
        Cr = VehicleParameters.Cr
        Ts = FixedControllerParameters.Ts

        x, y, yaw, v_x, v_y, yaw_dot = x_k[0], x_k[1], x_k[2], x_k[3], x_k[4], x_k[5]

        # Convert commands to physical values.
        Fx = self.Fx(u_k[0], v_x)
        delta = self.steer_cmd_to_angle(u_k[1], v_x, v_y)

        # Calculate slip angles.
        theta_Vf = ca.atan2((v_y + lf * yaw_dot), v_x+0.1)
        theta_Vr = ca.atan2((v_y - lr * yaw_dot), v_x+0.1)

        # Calculate lateral forces at front and rear using linear tire model.
        Fyf = Cf * (delta - theta_Vf)
        Fyr = Cr * (-theta_Vr)

        # Dynamics equations
        # See "Online Learning of MPC for Autonomous Racing" by Costa et al
        v_x_dot = ( (Fx - Fyf*ca.sin(delta)) / m ) + (v_y * yaw_dot)
        v_y_dot = ((Fyf*ca.cos(delta) + Fyr) / m) - (v_x * yaw_dot)
        yaw_dot_dot = ( (Fyf*ca.cos(delta)*lf) - (Fyr*lr)) / Iz

        # Integrate to find new state
        x_new = x + (v_x * ca.cos(yaw) - v_y * ca.sin(yaw)) * Ts
        y_new = y + (v_x * ca.sin(yaw) + v_y * ca.cos(yaw)) * Ts
        yaw_new = yaw + yaw_dot * Ts
        v_x_new = v_x + v_x_dot * Ts
        v_y_new = v_y + v_y_dot * Ts
        yaw_dot_new = yaw_dot + yaw_dot_dot * Ts

        state_new = ca.vertcat(x_new, y_new, yaw_new, v_x_new, v_y_new, yaw_dot_new)
        return state_new

    def steer_cmd_to_angle(self, steer_cmd, v_x, v_y):
        """
        Maps a steer command in [-1, 1] to the angle of the wheels
        over the next timestep, using CasADi operations.
        """
        vel = ca.sqrt(v_x**2 + v_y**2) * 3.6  # km/h

        # Define gain based on velocity via torque curve
        # gain = ca.if_else(vel < 20, 1.0, 
        #                 ca.if_else(vel < 60, 0.9, 
        #                             ca.if_else(vel < 120, 0.8, 0.7)))

        gain = 0.9
        return deg2rad(steer_cmd * gain * VehicleParameters.max_steer)

    def Fx(self, throttle, v_x):
        wheel_rpm = (v_x / VehicleParameters.C_wheel) * 60
        rpm = wheel_rpm * VehicleParameters.R * 4.5  # 4.5 is an estimated adjust based on observation

        eta = 0.6
        # eta = ca.if_else(rpm < 9000, 1.0,
        #          ca.if_else(rpm < 9500, 0.88,
        #                     ca.if_else(rpm < 10_400, 0.81,
        #                                ca.if_else(rpm < 12_500, 0.71, 0.675))))
        
        carla_penalty = normal_pdf(throttle, mean=0.5, variance=0.0775) * VehicleParameters.m

        wheel_force = throttle*eta*VehicleParameters.T_max*VehicleParameters.R / VehicleParameters.r_wheel
        drag_force = 0.5*VehicleParameters.rho*VehicleParameters.C_d*VehicleParameters.A_f*(v_x**2)
        rolling_resistance = VehicleParameters.C_roll*VehicleParameters.m*VehicleParameters.g

        return wheel_force - drag_force - rolling_resistance - carla_penalty

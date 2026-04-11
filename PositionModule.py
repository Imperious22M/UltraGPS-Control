# Contains the mathematical functions used to calculate the position of the transmiter/vehicle

import numpy as np
from scipy.optimize import least_squares


class PositionModule:
    def __init__(self, receiver_positions, max_differential=30, receiver_offsets=None):
        """
        Helper class that contains all the methods for calculating transmitter position
        using the receiver known coordinates and receiver measured distances

        Args:
            receiver_coordinates (list): 2D list of x,y coordinates of all receivers
                Must be in order (index 0 -> Receiver 1)
            max_differential (float): Maximum allowed change in distance (cm/s) for a
                receiver to be considered "sane". Default is 30.
            receiver_offsets (dict): Optional dictionary of receiver offsets
                {receiver_id: {'slope': a, 'intercept': b}}
        """

        # Sort incoming position array by the embedded tower id
        self.receiver_coordinates = np.array([cord for id,cord in sorted(receiver_positions) ])
        self.receiver_count = len(self.receiver_coordinates)
        self.max_differential = max_differential
        self.last_distances = None
        self.last_good_position = None
        self.last_sane_indices = None

        # Store receiver offsets for serial-to-distance conversion
        self.receiver_offsets = receiver_offsets or {}

        # Calculate maximum distance between any two receivers
        # This is used to validate that reported distances are within arena bounds
        self.max_receiver_distance = self._calculate_max_receiver_distance()
        #print(self.receiver_coordinates)
        #print(self.receiver_count)

    def set_receiver_offsets(self, offsets):
        """
        Set the receiver offsets for serial-to-distance conversion.

        Args:
            offsets (dict): {receiver_id: {'slope': a, 'intercept': b}}
        """
        self.receiver_offsets = offsets

    def serial_to_distances(self, serial_message):
        """
        Convert serial message values to distances using calibrated offsets.

        For each receiver: distance = ticks * a + b
        where a = slope and b = intercept from calibration.

        This method is compatible with the same format returned by
        ControlModule.get_receiver_distances().

        Args:
            serial_message (str): Space-separated serial values from receivers,
                or list of serial values

        Returns:
            tuple: Distances for each receiver in the same format as get_receiver_distances()
        """
        # Parse serial message if it's a string
        if isinstance(serial_message, str):
            serial_values = serial_message.strip().split(", ")
            
            try:
                serial_values = [float(v) for v in serial_values]
            except ValueError as e:
                print(f"Error parsing serial message: {e}")
                return tuple([0.0] * self.receiver_count)
        else:
            serial_values = list(serial_message)

        # Ensure we have enough values
        if len(serial_values) < self.receiver_count:
            print(f"Not enough serial values: got {len(serial_values)}, expected {self.receiver_count}")
            return tuple([0.0] * self.receiver_count)

        distance_list = []
        for recv_id in range(self.receiver_count):
            ticks = serial_values[recv_id]

            # Get offset parameters for this receiver
            if recv_id in self.receiver_offsets:
                a = self.receiver_offsets[recv_id].get('slope', 1.0)
                b = self.receiver_offsets[recv_id].get('intercept', 0.0)
            else:
                # Default: no calibration (distance = ticks)
                a = 1.0
                b = 0.0

            # Calculate distance
            distance = ticks * a + b
            distance_list.append(distance)

        return tuple(distance_list)

    def _calculate_max_receiver_distance(self):
        """
        Calculate the maximum distance between any two receivers.
        This represents the maximum valid distance a receiver could report.
        """
        max_dist = 0
        for i in range(self.receiver_count):
            for j in range(i + 1, self.receiver_count):
                dist = np.linalg.norm(self.receiver_coordinates[i] - self.receiver_coordinates[j])
                if dist > max_dist:
                    max_dist = dist
        return max_dist

    def filter_receivers(self, receiver_distances):
        """
        Filter receivers based on:
        1. The differential (change from last measurement) - must be less than max_differential
        2. The distance value - must be less than max_receiver_distance (arena bounds)

        A "sane" receiver passes both checks.

        Args:
            receiver_distances (list or np.ndarray): List of all distances from receivers

        Returns:
            np.ndarray: Indices of receivers considered "sane"
        """
        if not isinstance(receiver_distances, np.ndarray):
            receiver_distances = np.array(receiver_distances)

        # Check which receivers report distances within arena bounds
        within_bounds = receiver_distances <= self.max_receiver_distance

        # On first call, only check bounds (no previous data for differential)
        if self.last_distances is None:
            self.last_distances = receiver_distances.copy()
            sane_indices = np.where(within_bounds)[0]
            self.last_sane_indices = sane_indices
            return sane_indices

        # Calculate the differential for each receiver
        differentials = np.abs(receiver_distances - self.last_distances)

        # Find indices where differential is below threshold AND distance is within bounds
        sane_mask = (differentials < self.max_differential) & within_bounds
        sane_indices = np.where(sane_mask)[0]

        # Update last distances for next call
        self.last_distances = receiver_distances.copy()

        # Store sane indices for external access
        self.last_sane_indices = sane_indices

        return sane_indices

    def multilateration_method_1(self, receiver_distances, receiver_indices):
        """
        Calculates the initial position of a transmitter based on a list of distances

        Args:
            receiver_distances (list): List of all distances from the receivers to the vehicle
                Must be in order (index 0 -> Receiver 1)
            receiver_indices (list): Indices of receivers to use for calculation

        Returns:
            tuple: (position, result) where position is [x, y] and result is the optimization result.
                   If fewer than 3 sane receivers, returns (last_good_position, None).
        """
        # Filter receivers based on differential
        sane_indices = self.filter_receivers(receiver_distances)

        # Intersect sane indices with requested receiver indices
        filtered_indices = np.array([i for i in receiver_indices if i in sane_indices])

        # Check if we have at least 3 sane receivers
        if len(filtered_indices) < 3:
            if self.last_good_position is not None:
                return (self.last_good_position, None)
            else:
                # No last good position, fall back to using all requested indices
                filtered_indices = np.array(receiver_indices)

        initial_position = self.ordinary_least_squares(receiver_distances, filtered_indices)
        position_non_linear, result = self.non_linear_least_squares(receiver_distances, initial_position, filtered_indices)

        # Save as last good position
        self.last_good_position = position_non_linear.copy()

        return (position_non_linear, result)

    def get_position(self, serial_message):
        """
        Full pipeline: convert a raw serial/UDP message into a 2D position.

        Chains serial_to_distances() → multilateration_method_1() in one call,
        running the differential filter and OLS-seeded Levenberg-Marquardt solver
        internally.  All intermediate values are returned for diagnostics.

        Args:
            serial_message (str | list): Raw tick values from the UDP/TCP stream.
                Accepts the already-stripped content (no "N: " or "C: " prefix)
                or a plain list/tuple of float tick values.

        Returns:
            dict:
                'position'     : np.ndarray [x, y] in cm, or None on failure
                'distances'    : tuple of per-receiver distances (cm)
                'sane_indices' : list[int] of receivers that passed sanity checks
                'residual_rms' : float RMS of solver residuals (cm), or None
                'success'      : bool — True when the solver converged with >= 3
                                  sane receivers
        """
        distances = self.serial_to_distances(serial_message)
        position, result = self.multilateration_method_1(
            distances, list(range(self.receiver_count))
        )

        rms = (
            float(np.sqrt(np.mean(result.fun ** 2)))
            if result is not None
            else None
        )
        success = result is not None and result.success

        return {
            "position":     position,
            "distances":    distances,
            "sane_indices": list(self.last_sane_indices)
                            if self.last_sane_indices is not None else [],
            "residual_rms": rms,
            "success":      success,
        }

    # ~~~~~~ Internal Math functions ~~~~~~~

    # Ordinary Linear Least Squares Solution to the system of linear equations
    def ordinary_least_squares(self, receiver_distances, indices):

        if not isinstance(receiver_distances,np.ndarray):
            receiver_distances = np.array(receiver_distances)
        # At least 3 transmitters need to be used
        if len(indices)<3:
            raise(ValueError)

        # Pick the receivers to be used for calculation
        receiver_coordinates = self.receiver_coordinates[indices]
        receiver_distances = receiver_distances[indices]

        # Ideally x1/y1 would be from the receiver with the smallest distance (to minimize error)
        x1, y1 = receiver_coordinates[0]
        d1_sq = receiver_distances[0]**2

        A = []
        b = []
        # Build the A and b matrices
        for i in range(1, len(receiver_distances)):
            xi, yi = receiver_coordinates[i]
            A.append([2*(x1 - xi), 2*(y1 - yi)])
            b.append(receiver_distances[i]**2 - d1_sq - (xi**2 + yi**2) + (x1**2 + y1**2))

        A = np.array(A)
        b = np.array(b)

        # Solve linear least squares
        # [0] => x-cord
        # [1] => y-cord
        x_linear = np.linalg.lstsq(A, b, rcond=None)[0]

        return x_linear

    # Non-linear least squares estimate
    def non_linear_least_squares(self, receiver_distances, initial_estimate, indices):
        """ 
            Iterative method of obtaining a linear solution by minimzing a cost function
            using a least-squares approach

            Args:
                receiver_coordinates: Ordered list of (x,y) coordinates of all receivers
                receiver_distances: Ordered list of all measured distances
                initial_estimate: Initial position estimate (x,y), obtained by some other method
        """

        if not isinstance(receiver_distances,np.ndarray):
            receiver_distances = np.array(receiver_distances)
        # At least 3 transmitters need to be used
        if len(indices)<3:
            raise(ValueError)

        # Pick the receivers to be used for calculation
        receiver_coordinates = self.receiver_coordinates[indices]
        receiver_distances = receiver_distances[indices]

        # Define residual function (cost function)
        def residuals(x, positions, measurements):
            """Compute residuals: predicted_distance - measured_distance"""
            return np.linalg.norm(positions - x, axis=1) - measurements

        # Solve nonlinear least squares
        result = least_squares(residuals, initial_estimate,
                                #loss='soft_l1',
                                args=(receiver_coordinates, 
                                receiver_distances),
                                method='lm')
        x_nonlinear = result.x
        # [0] => x-cord
        # [1] => y-cord
        return (x_nonlinear, result)

import numpy as np
from scipy.optimize import least_squares
from itertools import combinations

class StablePositionEstimator:
    def __init__(self, receiver_positions):
        self.receivers = [list(cords) for index,cords in receiver_positions]
        print(self.receivers)
        self.num_rx = len(receiver_positions)
        
    def estimate_with_cep(self, distances):
        """
        Main function: Returns stable position estimate
        """
        # Step 1: Try different transmitter combinations
        best_error = float('inf')
        best_pos = None
        
        # Always try all transmitters first
        combos_to_try = [list(range(self.num_rx))]
        
        # Try removing one transmitter at a time
        for i in range(self.num_rx):
            combos_to_try.append([j for j in range(self.num_rx) if j != i])

        # Try removing the two noisiest-looking ones
        # (based on largest residuals from all-transmitter solution)
        all_pos = self.solve_position(distances, list(range(self.num_rx)) )
        receivers_arr = np.array(self.receivers)
        distances_arr = np.array(distances)
        residuals = np.abs(np.linalg.norm(receivers_arr - all_pos, axis=1) - distances_arr)
        
        noisy_indices = np.argsort(residuals)[-2:]  # Two with largest residuals
        combos_to_try.append([i for i in range(self.num_rx) if i not in noisy_indices])
        
        # Evaluate each combination
        for indices in combos_to_try:
            if len(indices) >= 3:  # Need at least 3 for 2D
                pos = self.solve_position(distances, indices)
                
                # Calculate "quality score" = position consistency
                # (not formal CEP but effective)
                pred_dists = np.linalg.norm(self.receivers - pos, axis=1)
                print(pred_dists)
                print("done")
                print(indices)
                error = np.std(pred_dists[indices] - distances_arr[indices])
                
                print("done")
                if error < best_error:
                    best_error = error
                    best_pos = pos
        
        return best_pos
    
    def solve_position(self, distances, indices):
        """Solve for position using specific transmitters"""
        # Convert to numpy arrays for fancy indexing
        receivers_arr = np.array(self.receivers)
        distances_arr = np.array(distances)
        selected_rx = receivers_arr[indices]
        selected_dists = distances_arr[indices]
        # Centroid as initial guess
        initial_guess = np.mean(selected_rx, axis=0)
        
        def residuals(pos):
            return np.linalg.norm(selected_rx - pos, axis=1) - selected_dists
        
        result = least_squares(residuals, initial_guess, method='lm')
        return result.x

class CEPPositioning:
    def __init__(self, receiver_positions, min_transmitters=3, max_differential=30):
        """
        receiver_positions: (6, 2) array of receiver coordinates
        min_transmitters: Minimum number to use (3 for 2D, but 4 is more robust)
        max_differential: Maximum allowed change in distance (cm/s) for a receiver
            to be considered "sane". Default is 30.
        """
        self.receiver_positions = receiver_positions
        receiver_coordinates = [cords for index,cords in receiver_positions]
        self.receivers = np.array(receiver_coordinates)
        self.num_receivers = len(receiver_positions)
        self.min_transmitters = min_transmitters
        self.max_differential = max_differential
        self.last_distances = None
        self.last_good_position = None
        self.last_good_cep = None
        self.last_good_indices = None
        self.last_good_cov = None
        self.last_sane_indices = None

        # Calculate maximum distance between any two receivers
        # This is used to validate that reported distances are within arena bounds
        self.max_receiver_distance = self._calculate_max_receiver_distance()

    def _calculate_max_receiver_distance(self):
        """
        Calculate the maximum distance between any two receivers.
        This represents the maximum valid distance a receiver could report.
        """
        max_dist = 0
        for i in range(self.num_receivers):
            for j in range(i + 1, self.num_receivers):
                dist = np.linalg.norm(self.receivers[i] - self.receivers[j])
                if dist > max_dist:
                    max_dist = dist
        return max_dist

    def filter_receivers(self, receiver_distances):
        """
        Filter receivers based on:
        1. The differential (change from last measurement) - must be less than max_differential
        2. The distance value - must be less than max_receiver_distance (arena bounds)

        A "sane" receiver passes both checks.

        Args:
            receiver_distances (list or np.ndarray): List of all distances from receivers

        Returns:
            np.ndarray: Indices of receivers considered "sane"
        """
        if not isinstance(receiver_distances, np.ndarray):
            receiver_distances = np.array(receiver_distances)

        # Check which receivers report distances within arena bounds
        within_bounds = receiver_distances <= self.max_receiver_distance

        # On first call, only check bounds (no previous data for differential)
        if self.last_distances is None:
            self.last_distances = receiver_distances.copy()
            sane_indices = np.where(within_bounds)[0]
            self.last_sane_indices = sane_indices
            return sane_indices

        # Calculate the differential for each receiver
        differentials = np.abs(receiver_distances - self.last_distances)

        # Find indices where differential is below threshold AND distance is within bounds
        sane_mask = (differentials < self.max_differential) & within_bounds
        sane_indices = np.where(sane_mask)[0]

        # Update last distances for next call
        self.last_distances = receiver_distances.copy()

        # Store sane indices for external access
        self.last_sane_indices = sane_indices

        return sane_indices

    def compute_position_and_cep(self, distances, use_indices=None):
        """
        Compute position and CEP for a specific set of transmitters

        Returns: (position, CEP_radius, covariance_matrix, use_indices)
        """
        if use_indices is None:
            use_indices = list(range(self.num_receivers))

        distances = np.array(distances)

        pos_module = PositionModule(self.receiver_positions)
        # Pass full distances array - multilateration_method_1 will select by indices internally
        pos, result = pos_module.multilateration_method_1(distances, use_indices)

        # If result is None (returned last good position due to insufficient sane receivers),
        # return with high uncertainty
        if result is None:
            cov = np.eye(2) * 1000
            cep = 1000
            return pos, cep, cov, use_indices

        # Compute covariance matrix and CEP
        # Jacobian at solution gives sensitivity
        J = result.jac
        residuals_vec = result.fun

        # Estimate measurement variance from residuals
        n = len(use_indices)
        m = 2  # number of parameters (x, y)
        if n > m:
            # Weighted by inverse of residual magnitude
            W = np.diag(1.0 / (np.abs(residuals_vec) + 0.1))
            # Covariance: (J^T W J)^-1
            try:
                cov = np.linalg.inv(J.T @ W @ J)

                # CEP approximation for 2D Gaussian
                # CEP ≈ 0.59 * (σ_x + σ_y) for small covariance
                # More accurate: CEP = 0.59 * sqrt(σ_x² + σ_y²) * sqrt(2)
                sigma_x = np.sqrt(cov[0, 0])
                sigma_y = np.sqrt(cov[1, 1])
                cep = 0.59 * np.sqrt(sigma_x**2 + sigma_y**2) * np.sqrt(2)

                # Alternative: use 1.1774 * sqrt(average variance)
                # cep = 1.1774 * np.sqrt((sigma_x**2 + sigma_y**2) / 2)

            except np.linalg.LinAlgError:
                # Singular matrix - receivers in bad geometry
                cov = np.eye(2) * 1000  # Large uncertainty
                cep = 1000
        else:
            cov = np.eye(2) * 1000
            cep = 1000

        return pos, cep, cov, use_indices
    
    def find_best_subset(self, distances, max_subsets_to_try=None):
        """
        Try different transmitter combinations, return one with lowest CEP.
        Filters receivers based on differential before calculating.

        Returns: (best_position, best_cep, best_indices, best_cov, results)
                 If fewer than 3 sane receivers, returns last good values.
        """
        if max_subsets_to_try is None:
            max_subsets_to_try = 20  # Limit to avoid combinatorial explosion

        # Filter receivers based on differential
        sane_indices = self.filter_receivers(distances)

        # Check if we have at least 3 sane receivers
        if len(sane_indices) < 3:
            if self.last_good_position is not None:
                return (self.last_good_position, self.last_good_cep,
                        self.last_good_indices, self.last_good_cov, [])
            else:
                # No last good position, use all receivers as fallback
                sane_indices = np.arange(self.num_receivers)

        best_cep = float('inf')
        best_position = None
        best_indices = None
        best_cov = None

        # Try different combinations of sane transmitters only
        all_combinations = []
        sane_list = list(sane_indices)

        # Start with using all sane transmitters
        all_combinations.append(sane_list)

        # Try subsets of sane receivers of size min_transmitters and up
        for k in range(self.min_transmitters, len(sane_list)):
            for subset in combinations(sane_list, k):
                all_combinations.append(list(subset))
                if len(all_combinations) >= max_subsets_to_try:
                    break
            if len(all_combinations) >= max_subsets_to_try:
                break

        # Evaluate each combination
        results = []
        for indices in all_combinations:
            position, cep, cov, _ = self.compute_position_and_cep(distances, indices)
            results.append({
                'position': position,
                'cep': cep,
                'indices': indices,
                'cov': cov,
                'num_transmitters': len(indices)
            })

            if cep < best_cep:
                best_cep = cep
                best_position = position
                best_indices = indices
                best_cov = cov

        # Sort by CEP for analysis
        results.sort(key=lambda x: x['cep'])

        # Save as last good values
        if best_position is not None:
            self.last_good_position = best_position.copy() if hasattr(best_position, 'copy') else best_position
            self.last_good_cep = best_cep
            self.last_good_indices = best_indices
            self.last_good_cov = best_cov

        return best_position, best_cep, best_indices, best_cov, results

    def validate_position(self, position):
        """
        Validate that the predicted position is within the bounds of the receiver coordinates.
        The position must have X and Y values within the min/max X and Y of all receivers.

        Args:
            position: Array-like [x, y] position to validate

        Returns:
            tuple: (validated_position, invalid_position)
                - validated_position: The input position if valid, or last_good_position if invalid
                - invalid_position: Boolean, True if position was outside bounds
        """
        if position is None:
            return self.last_good_position, True

        x, y = position[0], position[1]

        # Get receiver coordinate bounds
        x_min = self.receivers[:, 0].min()
        x_max = self.receivers[:, 0].max()
        y_min = self.receivers[:, 1].min()
        y_max = self.receivers[:, 1].max()

        # Check if position is within bounds
        is_valid = (x_min <= x <= x_max) and (y_min <= y <= y_max)

        if is_valid:
            return position, False
        else:
            # Position is outside bounds, return last good position
            if self.last_good_position is not None:
                return self.last_good_position, True
            else:
                # No last good position available, return the invalid position anyway
                return position, True

    def adaptive_weighted_solution(self, distances, history_length=10):
        """
        Adaptive method: Learn which transmitters are consistently noisy
        by tracking their contribution to CEP over time
        """
        # Initialize weights based on recent performance
        if not hasattr(self, 'weight_history'):
            self.weight_history = []
            self.receiver_weights = np.ones(self.num_receivers)
        
        # Get current best subset
        current_position, current_cep, best_indices, cov, all_results = \
            self.find_best_subset(distances, max_subsets_to_try=15)
        
        # Update weight history
        weight_update = np.zeros(self.num_receivers)
        weight_update[list(best_indices)] = 1.0
        
        self.weight_history.append(weight_update)
        if len(self.weight_history) > history_length:
            self.weight_history.pop(0)
        
        # Compute average reliability
        if len(self.weight_history) >= 5:  # Need some history
            reliability = np.mean(self.weight_history, axis=0)
            # Smooth weights: alpha * old + (1-alpha) * new
            alpha = 0.8
            self.receiver_weights = alpha * self.receiver_weights + (1-alpha) * reliability
        
        # Use weights in final weighted solution
        def weighted_residuals(pos):
            pred = np.linalg.norm(self.receivers - pos, axis=1)
            residuals = pred - distances
            return residuals * np.sqrt(self.receiver_weights)
        
        # Use current best as initial guess
        result = least_squares(weighted_residuals, current_position, method='lm')
        final_position = result.x
        
        # Compute final CEP with all transmitters (weighted)
        J = result.jac
        residuals_vec = result.fun
        W = np.diag(self.receiver_weights)
        try:
            cov_final = np.linalg.inv(J.T @ W @ J)
            sigma_x = np.sqrt(cov_final[0, 0])
            sigma_y = np.sqrt(cov_final[1, 1])
            final_cep = 0.59 * np.sqrt(sigma_x**2 + sigma_y**2) * np.sqrt(2)
        except:
            final_cep = current_cep
        
        return final_position, final_cep, self.receiver_weights, best_indices


import numpy as np
from scipy.optimize import least_squares
from collections import deque
from filterpy.kalman import KalmanFilter

class StabilizedPositioningSystem:
    def __init__(self, receiver_positions, initial_guess):
        self.receivers = [list(pos) for id,pos in receiver_positions]
        print(self.receivers)
        self.num_receivers = len(receiver_positions)
        
        # Weight tracking (learn receiver reliability)
        self.receiver_weights = np.ones(self.num_receivers)
        self.residual_history = deque(maxlen=100)
        
        # Position history for smoothing
        self.position_history = deque(maxlen=10)
        self.last_position = np.array(initial_guess)
        
        # Kalman filter (if you have time/speed info)
        self.use_kalman = False
        if self.use_kalman:
            #from filterpy.kalman import KalmanFilter
            self.kf = self._init_kalman_filter(initial_guess)
    
    def _init_kalman_filter(self, initial_pos):
        kf = KalmanFilter(dim_x=4, dim_z=2)
        kf.x = np.array([initial_pos[0], initial_pos[1], 0, 0])  # [x, y, vx, vy]
        
        # Constant velocity model
        dt = 0.1  # Time step
        kf.F = np.array([[1, 0, dt, 0],
                        [0, 1, 0, dt],
                        [0, 0, 1, 0],
                        [0, 0, 0, 1]])
        
        kf.H = np.array([[1, 0, 0, 0],
                        [0, 1, 0, 0]])
        
        kf.P *= 10  # Initial uncertainty
        kf.R = np.eye(2) * 0.5  # Measurement noise
        kf.Q = np.eye(4) * 0.1  # Process noise
        
        return kf
    
    def estimate_position(self, distances):
        # Step 1: Get initial estimate with current weights
        receivers_arr = np.array(self.receivers)
        distances_arr = np.array(distances)
        def residuals(tower_pos):
            predicted = np.linalg.norm(receivers_arr - tower_pos, axis=1)
            return np.sqrt(self.receiver_weights) * (predicted - distances_arr)
        
        # Use previous position as initial guess (warm start)
        result = least_squares(
            residuals,
            self.last_position,
            #method='lm',
            loss='soft_l1',  # Robust to outliers
            f_scale=0.5
        )
        
        raw_estimate = result.x
        
        # Step 2: Update receiver weights based on residuals
        current_residuals = np.abs(result.fun)
        self.residual_history.append(current_residuals)
        
        # Calculate moving average of residuals for each receiver
        if len(self.residual_history) > 10:
            recent_residuals = np.array(self.residual_history)[-10:]
            avg_residuals = np.mean(recent_residuals, axis=0)
            
            # Update weights: lower weight for receivers with high average residuals
            self.receiver_weights = 1.0 / (avg_residuals + 0.1)
            self.receiver_weights = self.receiver_weights / np.max(self.receiver_weights)
        
        # Step 3: Apply Kalman filtering if enabled
        if self.use_kalman:
            self.kf.predict()
            self.kf.update(raw_estimate)
            filtered_estimate = self.kf.x[:2]
        else:
            # Simple moving average
            self.position_history.append(raw_estimate)
            filtered_estimate = np.mean(self.position_history, axis=0)
        
        print("done")
        # Step 4: Validate position
        #filtered_estimate = self._validate_position(filtered_estimate)
        
        # Step 5: Update last position
        self.last_position = filtered_estimate
        
        return filtered_estimate
    
    def _validate_position(self, new_pos):
        """Sanity checks on position"""
        # 1. Check if within reasonable bounds
        bounds_margin = 50
        receivers_arr = np.array(self.receivers)
        x_min, x_max = receivers_arr[:, 0].min(), receivers_arr[:, 0].max()
        y_min, y_max = receivers_arr[:, 1].min(), receivers_arr[:, 1].max()
        
        x_bounded = np.clip(new_pos[0], x_min - bounds_margin, x_max + bounds_margin)
        y_bounded = np.clip(new_pos[1], y_min - bounds_margin, y_max + bounds_margin)
        
        return np.array([x_bounded, y_bounded])

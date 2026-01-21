import dis
from PositionModule import PositionModule
import SettingsModule

import numpy as np
from scipy.optimize import least_squares


import time

class PositionModule:
    def __init__(self, receiver_positions):
        """
        Helper class that contains all the methods for calculating transmitter position
        using the receiver known coordinates and receiver measured distances

        Args:
            receiver_coordinates (list): 2D list of x,y coordinates of all receivers
                Must be in order (index 0 -> Receiver 1)
        """

        # Sort incoming position array by the embedded tower id
        self.receiver_coordinates = np.array([cord for id,cord in sorted(receiver_positions) ])
        self.receiver_count = len(self.receiver_coordinates)
        #print(self.receiver_coordinates)
        #print(self.receiver_count)

    def multilateration_method_1(self, receiver_distances, receiver_indices):
        """
        Calculates the initial position of a transmitter based on a list of distances

        Args:
            receiver_positions (list): List of all distances from the receivers to the vehicle
            Must be in order (index 0 -> Receiver 1)
        """
        print(receiver_indices)
        initial_position = self.ordinary_least_squares(receiver_distances,receiver_indices)
        print(initial_position)
        position_non_linear, result = self.non_linear_least_squares(receiver_distances, initial_position, receiver_indices)

        return (position_non_linear, result)

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
        print("Nonlinear LS estimate:", x_nonlinear)
        return (x_nonlinear, result)

pos = SettingsModule.SettingsModule()
tower_pos = pos.get_tower_coordinates()

distances = [59.4103, 206.322, 34.8583, 47.4073, 78.3547, 42.1883]

pos_module = PositionModule(tower_pos)

print(pos_module.multilateration_method_1(distances,list(range(0,6)))[0] )
#print(pos_module.multilateration_method_1(distances,[0,1,3,4,5]) )
print(pos_module.multilateration_method_1(distances,[0,2,3,4,5])[0] )
print(pos_module.multilateration_method_1(distances,[0,2,5])[0] )

#print(pos_module.multilateration_method_1(distances,list(range(1,6))))
#print(pos_module.multilateration_method_1(distances,list(range(2,6))))
#print(pos_module.multilateration_method_1(distances,list(range(3,6))))
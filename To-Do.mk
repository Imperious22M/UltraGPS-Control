Description of task to be accomplished:

#Graphical Changes and Logical Sequence

On the left side a title should be made (with a label) called "Run 1", this will
correspond to the first calibration run, described in the next section. Below 
the title six histogram plots should be created in the following manner:
	3	6
	2	5
	1	4
The left row below the Run 1 title should contain three histogram 
labeled, from top to bottom, "Receiver 3 Serial", "Receiver 4 Serial", and "Receiver 5 Serial",
on the right row below the Run 1 there should be another three histograms
with the labels, from top to bottom, "Receiver 6 Serial", "Receiver 5 Serial", "Receiver 4 Serial".
This entire histogram assembly should be on the right hand side of the position arena.

On the right hand side of the position arena, an identical set of histograms should be made with the title
"Run 2" with an identical arangement of histograms.

Below the position window a rectangular window should be created that contains the title
"Ax + b Receiver Offsets". This window should contain the same numerical arrangements
as Run 1 and Run 2, meaning that the values for receiver 3, 2 and 1 should be shown
in the left column from top to bottom (3,2,1), and the values for receiver 6,5, and 4
should be shown in the right hand side in the same manner. When it is first shown it should
read the values that are stored in the toml file for each receiver (intercept = b, 
slop = 1) and update the text with their values in the following manner: 
"Recvr Offset: ax + b", where a and b are replaced.
 

A button should be created in the Calibration Window, centered above the 
position arena that says 'Start Calibration'. This button should create a popup that asks the user 
to place the transmitter at the first calibration point. The first calibration point,
which is stored in the toml file, should now be shown in the same manner as
the way it is shown in the arena maker window (a green diamond). The popup should
have two choices: "Ready" and "Cancel". The "Ready" option should trigger
the first calibration run, shown below, while the "Cancel" option should cancel the entire routine.

When the first calibration run is finished, and the histograms for that run have
finished updating, show another popup that contains the same options as the popup
shown before. When the "Ready" button is pressed it should run the second calibration run

Whent the second calibration run is finished, the a and b offsets should be calculated using 
the serial values that have been received the most from each run (should be at the top of the dictionary).
The calculation process is shown in the "Offset Calculation" Section

These a and b offsets should be calculated for each receiver using the calculation
methodology shown in the section "Offset Calculation" and should be stored for each
receiver in the toml file under each receiver's "intercept", which equals b, and
the "slope", which equals a. Once these values are stored Update the Ax+b window 
that is located below the arena with the values for each receiver (aranged in the same manner
as the run 1 and run 2 histogram plots) in the manner: "Recvr Offset: ax + b", 
where a is replaced with the value of the slope and b is replaced with the value of the intercept. 


#Calibration Process/Run:

When a user presses the "Ready" button in the popup, the following 
should occur:
	1) An initial serial message should be requested and ignored simply to
	   clear the network when the process starts. 
	   Then serial messages need to be collected and 
	   stored in a dictionary for each receiver. This means that for every
	   serial message that is received from the UDP server the serial
	   message for each receiver should be stored in a dictionary in the manner
	   explained below.
	2) As the serial messages are being received the dictionary in which
	   they are stored should automatically reshuffle to keep the most frequent
	   serial message that occurs at the top of the list, something like so:
		dictionary{ number of occurences: serial message}, with the number of occurences
		being the number of times that exact serial message has shown up.
	3) At the same time that the dictionaries for all the receivers are being
	   made they should be plotted by a histogram that has the x-axis be the 
	   serial values that have been received by that receiver, which should re-
	   size whenever a new value is received, and the y-axis be the number of times
	   that value was received, which should also be re-sized whenever a value is
	   received.
	4) This process should stop when the most frequent value for each receiver
	   is equal or greater than the minimum number required by the calibration 
	   process, which is passed as an argument called min_reads.
After the min_reads have been met, this method should return

# Offset calculation
The offsets (a = slop, b = intercept) should be calaculated in the following manner
	for each receiver do the following:
		known_dist_1 =  the first known distance of that receiver (gotten from the toml file)
		known_dist_2 = the second known distance of that receiver
		valid_count_1 = the serial message that has the most appeareances for that receiver in run 1
		valid_count_2 = the serial_message that has the most appeareances for the receiver in run 2
		a = (known_dist_1 - known_dist_2) / (valid_count_1 - valid_count_2)
		b = (known_dist_1 - (a*valid_count_1))
		receiver.intercept = a
		receiver.slope  = b
		save the a and b offsets in the toml file
The function used to calculate this should be stored in the CalibrateSystem class. A standalone
function that performs the runs and acquires the most common serial values for the runs should also
be made in this class. It should not use any graphical elements and simply be standalone, requiring
just the number of reads (minimum number of frequency per reading that is needed to fulfill a run)
that is needed to complete a run and access to the control module for communication, which
should be given at construction. Do not use this function but simply make it available.

# Final Steps

When all the above is done, modify the PositionModule class to add a method
that can take the Serial messages and using them returns the distances for each
receiver by doing the following algorithm:
	for each receiver
		ticks = serial read for the receiver
		distance = ticks*a + b
		distance_list.append(distance)
	return distance_list
Make this compatible with the same way that distances are returned from get_receiver_distances.
Comment the line with _get_receiver_distances and below put the distance calculation function made
as a drop-in replacement. 



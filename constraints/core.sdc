# 100 MHz target, Nangate45 open cell library.
create_clock -name clk -period 10.0 [get_ports clk]

# Setup margin for a first pre-CTS run.  Hold uncertainty is 0 here because the
# clock is ideal (no CTS); re-run hold signoff after clock-tree insertion.
set_clock_uncertainty -setup 0.5 [get_clocks clk]
set_clock_uncertainty -hold 0.0 [get_clocks clk]

# OpenSTA 3.1 does not provide remove_from_collection; list the data inputs
# explicitly and keep clk out of the input-delay set.
set_input_delay 2.0 -clock clk [get_ports {bus_rdata_i[*] bus_ready_i timer_irq_i ext_irq_i soft_irq_i}]
set_output_delay 2.0 -clock clk [all_outputs]
set_load 0.05 [all_outputs]

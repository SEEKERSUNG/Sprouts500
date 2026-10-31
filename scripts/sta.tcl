# OpenSTA script: run from the project root with:
#   bash scripts/sta.sh
read_liberty lib/NangateOpenCellLibrary_typical.lib
read_verilog build/syn/core_netlist.v
link_design core
read_sdc constraints/core.sdc

puts "\n===== max path ====="
report_checks -path_delay max -format full_clock_expanded -digits 3
puts "\n===== min path ====="
report_checks -path_delay min -format full_clock_expanded -digits 3
puts "\n===== summary ====="
report_tns -max
report_wns -max
report_tns -min
report_wns -min

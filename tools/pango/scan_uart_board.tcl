# Read-only JTAG identity scan. No FPGA configuration or nonvolatile operations.
cfg_set_tcl_break -flag true
cfg_connect -ip 127.0.0.1 -port 65421
cfg_scan_chain
cfg_read_device_property -mode 0 -device_index 0
cfg_disconnect
exit

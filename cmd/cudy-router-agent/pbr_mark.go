package main

import (
	"strconv"
	"strings"
)

// pbrMarkForInterfaceRules resolves the fwmark assigned by OpenWrt pbr to an
// interface-specific routing table, for example pbr_proxyde.
func pbrMarkForInterfaceRules(output, iface string) (int, bool) {
	table := "pbr_" + iface
	for _, line := range strings.Split(output, "\n") {
		fields := strings.Fields(line)
		markText := ""
		lookupTable := ""
		for index, field := range fields {
			switch field {
			case "fwmark":
				if index+1 < len(fields) {
					markText = strings.SplitN(fields[index+1], "/", 2)[0]
				}
			case "lookup":
				if index+1 < len(fields) {
					lookupTable = fields[index+1]
				}
			}
		}
		if markText == "" || lookupTable != table {
			continue
		}
		value, err := strconv.ParseUint(markText, 0, 32)
		if err == nil && value != 0 {
			return int(value), true
		}
	}
	return 0, false
}

//go:build linux

package main

import (
	"net"
	"os/exec"
	"syscall"
)

func bindDialerToInterface(dialer *net.Dialer, iface string) error {
	if iface == "" {
		return nil
	}
	mark := 0
	if output, err := exec.Command("ip", "-4", "rule", "show").Output(); err == nil {
		mark, _ = pbrMarkForInterfaceRules(string(output), iface)
	}
	dialer.Control = func(_, _ string, raw syscall.RawConn) error {
		var socketErr error
		if err := raw.Control(func(fd uintptr) {
			if mark != 0 {
				socketErr = syscall.SetsockoptInt(int(fd), syscall.SOL_SOCKET, syscall.SO_MARK, mark)
				if socketErr != nil {
					return
				}
			}
			socketErr = syscall.SetsockoptString(int(fd), syscall.SOL_SOCKET, syscall.SO_BINDTODEVICE, iface)
		}); err != nil {
			return err
		}
		return socketErr
	}
	return nil
}

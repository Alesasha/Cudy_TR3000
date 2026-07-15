//go:build linux

package main

import (
	"net"
	"syscall"
)

func bindDialerToInterface(dialer *net.Dialer, iface string) error {
	if iface == "" {
		return nil
	}
	dialer.Control = func(_, _ string, raw syscall.RawConn) error {
		var socketErr error
		if err := raw.Control(func(fd uintptr) {
			socketErr = syscall.SetsockoptString(int(fd), syscall.SOL_SOCKET, syscall.SO_BINDTODEVICE, iface)
		}); err != nil {
			return err
		}
		return socketErr
	}
	return nil
}

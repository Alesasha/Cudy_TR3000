//go:build !linux

package main

import "net"

func bindDialerToInterface(_ *net.Dialer, _ string) error {
	return nil
}

//go:build darwin || linux

package main

import "syscall"

func currentProcessCPU() *processCPU {
	var usage syscall.Rusage
	if err := syscall.Getrusage(syscall.RUSAGE_SELF, &usage); err != nil {
		return nil
	}
	return &processCPU{
		UserUS:   usage.Utime.Sec*1_000_000 + int64(usage.Utime.Usec),
		SystemUS: usage.Stime.Sec*1_000_000 + int64(usage.Stime.Usec),
	}
}

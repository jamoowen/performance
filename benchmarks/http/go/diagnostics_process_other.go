//go:build !darwin && !linux

package main

func currentProcessCPU() *processCPU {
	return nil
}

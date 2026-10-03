package main

import "runtime"

func runtimeVersionRaw() string { return runtime.Version() }

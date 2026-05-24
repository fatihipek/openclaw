#!/usr/bin/env python3
"""Atilgan Voice Service — DISABLED. Ses biyometrisi kapatıldı."""
import time
import signal
import sys

def handler(sig, frame):
    sys.exit(0)

signal.signal(signal.SIGTERM, handler)
signal.signal(signal.SIGINT, handler)

# Just sleep forever - 0 RAM usage
while True:
    time.sleep(3600)

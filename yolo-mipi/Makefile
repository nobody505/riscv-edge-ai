.DEFAULT_GOAL := all

CXX ?= g++
CC ?= gcc
SPACEMIT_ORT ?= $(HOME)/spacemit-ort.riscv64.2.0.1
CXXFLAGS := -std=c++17 -fPIC -w -g -pthread -DENABLE_DISPLAY=0 \
	-I/usr/include/opencv4 -I$(SPACEMIT_ORT)/include -Iinclude
LDFLAGS := -L$(SPACEMIT_ORT)/lib -L/usr/lib \
	-Wl,-rpath=$(SPACEMIT_ORT)/lib -Wl,-rpath='$$ORIGIN' \
	-lonnxruntime -lspacemit_ep \
	-lopencv_core -lopencv_imgproc -lopencv_imgcodecs -lopencv_highgui -lopencv_dnn \
	-lstdc++ -ldl -lpthread -lrt

.PHONY: all clean verify

all: workspace/consumer_final_new_gray hook_simple.so

workspace/consumer_final_new_gray: src/consumer_final_new_gray.cpp include/spacemit_ort_env.h
	@mkdir -p workspace
	$(CXX) $(CXXFLAGS) $< -o $@ $(LDFLAGS)

hook_simple.so: hook_simple.c
	$(CC) -shared -fPIC -O2 $< -o $@ -ldl -lpthread

verify: all
	file workspace/consumer_final_new_gray hook_simple.so
	readelf -d workspace/consumer_final_new_gray | grep -E 'onnxruntime|spacemit_ep|opencv'

clean:
	rm -f workspace/consumer_final_new_gray hook_simple.so

# EM-4 ファームウェア（ホスト向けのビルド）
#
#   make        build/em4fw を作る
#   make test   単体試験を作り、build/ で動かす
#   make clean  build/ を消す
#
# コンパイラーは PATH から呼ぶ（フルパスにしない）。
# 例) make CC=clang CXX=clang++

CC  ?= gcc
CXX ?= g++
# make に組み込みの既定値（CC=cc）のままなら gcc にする
ifeq ($(origin CC),default)
CC  := gcc
endif
ifeq ($(origin CXX),default)
CXX := g++
endif

BUILD    := build
OBJDIR   := $(BUILD)/obj
TARGET   := $(BUILD)/em4fw
TEST_BIN := $(BUILD)/test_em4

CPPFLAGS := -Iinclude -DEM4_HOST_BUILD=1 -MMD -MP
CFLAGS   := -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -g -Wall -Wextra
CXXFLAGS := -std=c++14 -O2 -g -Wall -Wextra -fno-exceptions -fno-rtti
LDFLAGS  :=
LDLIBS   :=

HAL_SRCS := \
	src/hal/sim_bus.c \
	src/hal/gpio.c \
	src/hal/timer.c

DRV_SRCS := \
	src/drivers/sht3x.c \
	src/drivers/bmp280.c \
	src/drivers/flash_store.c

PROTO_SRCS := \
	src/proto/crc16.c \
	src/proto/frame.c \
	src/proto/cmd_parser.c

APP_SRCS := \
	src/app/config_ini.c \
	src/app/logger.c \
	src/app/scheduler.c \
	src/app/fault.c

UTIL_SRCS := \
	src/util/ringbuf.c \
	src/util/strutil.c

TELEM_SRCS := \
	src/telemetry/stats.cpp \
	src/telemetry/telemetry.cpp

MAIN_SRC := src/app/main.c
TEST_SRC := tests/test_main.c

LIB_C_SRCS := $(HAL_SRCS) $(DRV_SRCS) $(PROTO_SRCS) $(APP_SRCS) $(UTIL_SRCS)
LIB_OBJS   := $(LIB_C_SRCS:%.c=$(OBJDIR)/%.o) $(TELEM_SRCS:%.cpp=$(OBJDIR)/%.o)
MAIN_OBJ   := $(MAIN_SRC:%.c=$(OBJDIR)/%.o)
TEST_OBJ   := $(TEST_SRC:%.c=$(OBJDIR)/%.o)

.PHONY: all test clean

all: $(TARGET)

# C++ の部品があるので、つなぐのは C++ のコンパイラーで行う
$(TARGET): $(LIB_OBJS) $(MAIN_OBJ)
	$(CXX) $(LDFLAGS) -o $@ $^ $(LDLIBS)

$(TEST_BIN): $(LIB_OBJS) $(TEST_OBJ)
	$(CXX) $(LDFLAGS) -o $@ $^ $(LDLIBS)

test: $(TEST_BIN)
	cd $(BUILD) && ./test_em4

$(OBJDIR)/%.o: %.c
	@mkdir -p $(dir $@)
	$(CC) $(CPPFLAGS) $(CFLAGS) -c $< -o $@

$(OBJDIR)/%.o: %.cpp
	@mkdir -p $(dir $@)
	$(CXX) $(CPPFLAGS) $(CXXFLAGS) -c $< -o $@

clean:
	rm -rf $(BUILD)

-include $(LIB_OBJS:.o=.d) $(MAIN_OBJ:.o=.d) $(TEST_OBJ:.o=.d)

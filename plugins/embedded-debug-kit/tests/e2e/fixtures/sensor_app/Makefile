CFLAGS ?= -O2 -Wall

sensor_app: main.c sensor.c sensor.h
	$(CC) $(CFLAGS) -o $@ main.c sensor.c

clean:
	rm -f sensor_app

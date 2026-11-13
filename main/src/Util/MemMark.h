#ifndef BUTTERBOT_FIRMWARE_MEMMARK_H
#define BUTTERBOT_FIRMWARE_MEMMARK_H

#include <cstdio>
#include <vector>
#include <esp_heap_caps.h>
#include <esp_memory_utils.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

/**
 * Fork: internal-RAM accounting on the serial console. Internal RAM is the robot's scarce resource:
 * when it runs out, Bluetooth / esp_timer allocations abort the whole robot (which then powers off).
 *   [BBMEM] <where> internal free N KB (largest M KB)
 *   [BBTASK] <task> stack=internal|psram unused=N B
 */
namespace MemMark {

inline void mark(const char* what){
	printf("[BBMEM] %-15s internal free %3u KB (largest %3u KB, lowest %3u KB)\n", what,
	       (unsigned) (heap_caps_get_free_size(MALLOC_CAP_INTERNAL) / 1024),
	       (unsigned) (heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL) / 1024),
	       (unsigned) (heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL) / 1024));
}

// Every task whose stack lives in internal RAM, with how much of it has never been touched.
inline void dumpTasks(){
	const UBaseType_t n = uxTaskGetNumberOfTasks();
	std::vector<TaskStatus_t> tasks(n + 4);
	const UBaseType_t got = uxTaskGetSystemState(tasks.data(), tasks.size(), nullptr);
	unsigned internalTasks = 0;
	for(UBaseType_t i = 0; i < got; i++){
		const TaskStatus_t& t = tasks[i];
		const bool internal = esp_ptr_internal(t.pxStackBase);
		if(internal) internalTasks++;
		printf("[BBTASK] %-16s stack=%-8s unused=%5u B\n", t.pcTaskName, internal ? "internal" : "psram",
		       (unsigned) t.usStackHighWaterMark);
	}
	printf("[BBTASK] %u tasks, %u with internal stacks\n", (unsigned) got, internalTasks);
}

}

#endif //BUTTERBOT_FIRMWARE_MEMMARK_H

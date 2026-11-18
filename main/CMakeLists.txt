file(GLOB_RECURSE LIBS "lib/*/src/**.cpp" "lib/*/src/**.c")

file(GLOB_RECURSE SOURCES
        "${CMAKE_CURRENT_SOURCE_DIR}/src/**.cpp"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/**.hpp"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/**.c"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/**.h"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*/**.cpp"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*/**.hpp"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*/**.c"
        "${CMAKE_CURRENT_SOURCE_DIR}/src/*/**.h"
        "${CMAKE_CURRENT_SOURCE_DIR}/main.cpp")

idf_component_register(SRCS ${SOURCES} ${LIBS} INCLUDE_DIRS "src")

spiffs_create_partition_image(storage ../spiffs FLASH_IN_PROJECT)

idf_build_get_property(BASEDIR PROJECT_DIR)
esptool_py_flash_to_partition(flash objdet ${BASEDIR}/models/objdet.tflite)

# Fork: the problem report's event log (src/Util/EventLog.cpp) sees the robot's "[BB...]" status lines by wrapping the
# C library's printf and puts (the compiler turns some printf calls into puts). Member functions called printf are
# unaffected.
target_link_libraries(${COMPONENT_LIB} INTERFACE "-Wl,--wrap=printf" "-Wl,--wrap=puts")

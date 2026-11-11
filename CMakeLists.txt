# The following lines of boilerplate have to be in your project's
# CMakeLists in this exact order for cmake to work correctly
cmake_minimum_required(VERSION 3.16)

set(CMAKE_CXX_STANDARD 23)

set(IDF_TARGET esp32s3)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(ButterBot-Firmware)
# The app is compiled for size (CONFIG_COMPILER_OPTIMIZATION_SIZE) because the 8.6 MB app partition
# was 98% full and the 16 MB flash has no room to grow it. These components do real-time or
# heavy compute (speech synthesis, object detection, face detection/recognition), so they keep -O2.
# A later -O flag wins, so this overrides the global -Os for just these libraries.
foreach(speed_critical flite espressif__esp-tflite-micro espressif__esp-nn espressif__esp-dl
                       espressif__human_face_detect espressif__human_face_recognition)
    if(TARGET __idf_${speed_critical})
        target_compile_options(__idf_${speed_critical} PRIVATE -O2)
    endif()
endforeach()

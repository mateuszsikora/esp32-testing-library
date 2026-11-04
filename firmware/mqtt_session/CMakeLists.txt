cmake_minimum_required(VERSION 3.19)
# Reuse the validation image's checksum-verified esp_modem 1.4.1 download.
# Keep dependencies in the writable build tree, including in the pinned container.
find_package(Python3 REQUIRED COMPONENTS Interpreter)
file(MAKE_DIRECTORY "${CMAKE_BINARY_DIR}/dependencies")
execute_process(
    COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_LIST_DIR}/../pppos_client/prepare.py"
            "${CMAKE_BINARY_DIR}/dependencies"
    OUTPUT_VARIABLE PPPOS_PROJECT OUTPUT_STRIP_TRAILING_WHITESPACE
    COMMAND_ERROR_IS_FATAL ANY)
get_filename_component(MODEM_COMPONENT "${PPPOS_PROJECT}/../.." ABSOLUTE)
set(EXTRA_COMPONENT_DIRS "${MODEM_COMPONENT}")
# All components are supplied above or by ESP-IDF; no registry resolution is needed.
set(ENV{IDF_COMPONENT_MANAGER} "0")
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(mqtt_session)

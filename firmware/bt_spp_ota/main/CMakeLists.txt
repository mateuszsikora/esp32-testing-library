# ESP-IDF's bt_spp_initiator example, its sources unchanged and built from the ESP-IDF tree, its
# app_main renamed spp_initiator_main; main.c here runs it in the boot that talks to the peer.
set(example $ENV{IDF_PATH}/examples/bluetooth/bluedroid/classic_bt/bt_spp_initiator/main)
idf_component_register(
    SRCS "main.c" "${example}/main.c" "${example}/app_spp_msg_prs.c" "${example}/app_spp_msg_set.c"
         "${example}/console_uart.c"
    INCLUDE_DIRS "." "${example}"
    REQUIRES app_update bootloader_support bt esp_driver_uart esp_partition nvs_flash spi_flash vfs)
set_source_files_properties("${example}/main.c" PROPERTIES COMPILE_DEFINITIONS "app_main=spp_initiator_main")

# bt_spp_ota_v2: bt_spp_ota's image, which firmware/build.sh builds first, beside this build's
# directory, copied into update/ in it and flashed into the update partition, for the first boot
# to copy into ota_1, with its ELF, for the test to name. In the build directory, flasher_args.json
# names the image relative to it, and the build holds the image and its ELF together: another
# build of bt_spp_ota is another image, its compile time in it. Not at the top of the build
# directory, where pytest-embedded takes the first .elf it lists for the application's.
if(CONFIG_BT_SPP_OTA_UPDATE)
    set(v1 ${CMAKE_BINARY_DIR}/../../bt_spp_ota/build/bt_spp_ota)
    if(NOT EXISTS ${v1}.bin OR NOT EXISTS ${v1}.elf)
        message(FATAL_ERROR "bt_spp_ota_v2 flashes bt_spp_ota's image, ${v1}.bin: build bt_spp_ota first")
    endif()
    configure_file(${v1}.bin ${CMAKE_BINARY_DIR}/update/bt_spp_ota.bin COPYONLY)
    configure_file(${v1}.elf ${CMAKE_BINARY_DIR}/update/bt_spp_ota.elf COPYONLY)
    esptool_py_flash_to_partition(flash "update" "${CMAKE_BINARY_DIR}/update/bt_spp_ota.bin")
endif()

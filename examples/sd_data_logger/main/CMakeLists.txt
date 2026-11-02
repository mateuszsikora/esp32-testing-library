set(NMEA_DIR "${CMAKE_CURRENT_LIST_DIR}/../../../firmware/nmea_reader/main")
idf_component_register(SRCS "main.c" "${NMEA_DIR}/nmea.c"
                       INCLUDE_DIRS "${NMEA_DIR}"
                       PRIV_REQUIRES esp_driver_uart esp_driver_gpio esp_driver_spi fatfs sdmmc)

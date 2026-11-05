idf_component_register(SRCS "main.c" INCLUDE_DIRS "."
                       PRIV_REQUIRES esp_driver_gpio esp_driver_uart esp_event esp_modem esp_netif lwip)

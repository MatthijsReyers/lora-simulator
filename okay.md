


```c++

Node::run()

LoRaRadio::transmit()

    LoRaPhy::transmit_data()
        LoRaPhy::__estimate_send_time()

        for each:
            LoraRadio::_receive_start()
                LoraRadio::__can_receive()
                LoraRadio::__find_overlap()


for each:
    LoRaPhy::_receive_end()

```
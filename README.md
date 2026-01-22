
## Example simulations

To demonstrate how to use the emulator and the various LoRa specific emulations tools provided by the simulator we have various example projects to demonstrate how to build simulation setups of increasing complexity. 
We recommend checking out the basic smaller examples first (listed at the top of the table below) and note that the more complex multi-file examples usually have their own README you can check out for more details.

| Name | File | Description |
| :--: | :--- | :---------- |
| Counters | `examples/counters.py` | Basic demonstration of how to run multiple tasks in the simulator simultaneously. |
| Queue | `examples/queue.py` | A basic demonstration to show how to use the Queue class provided by this simulator (note that you can *NOT* use a normal `asyncio.Queue` as the simulation might run for any amount of time while sending data between tasks).
| HTTP Request | `example/http_req.py` | Shows how to interleave real asynchronous work (like an HTTP request to a real LoRaWAN network server) safely within the simulator.


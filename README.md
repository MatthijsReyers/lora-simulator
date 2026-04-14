
## Example simulations

To demonstrate how to use the emulator and the various LoRa specific emulations tools provided by the simulator we have various example projects to demonstrate how to build simulation setups of increasing complexity. 
We recommend checking out the basic smaller examples first (listed at the top of the table below) and note that the more complex multi-file examples usually have their own README you can check out for more details.

| Name | File | Description |
| :--: | :--- | :---------- |
| Counters | `examples/counters.py` | Basic demonstration of how to run multiple tasks in the simulator simultaneously. |
| Child tasks | `examples/child_tasks` | Shows how to start new tasks/processes while the simulator is already running. |
| Queue | `examples/queue.py` | A basic demonstration to show how to use the Queue class provided by this simulator (note that you can *NOT* use a normal `asyncio.Queue` as the simulation might run for any amount of time while sending data between tasks).
| HTTP Request | `example/http_req.py` | Shows how to interleave real asynchronous work (like an HTTP request to a real LoRaWAN network server) safely within the simulator.
| LoRaWAN Clock Sync | `examples/lorawan_applications/clock_sync.py` | Demonstrates a TS003-style AppTime request/response service over FPort 202. |

## Unit testing

Those wishing to extend the simulator with new features for their own use should know that there are unit tests located in the `tests/` directory. 
Test files should follow the naming convention `test_*.py`.
You can run/debug the unit tests using the following commands:

```bash
# Run all tests
pipenv run pytest

# Run with verbose output
pipenv run pytest -v

# Run a specific test file
pipenv run pytest tests/test_environment.py

# Run tests with print output visible
pipenv run pytest -v -s
```


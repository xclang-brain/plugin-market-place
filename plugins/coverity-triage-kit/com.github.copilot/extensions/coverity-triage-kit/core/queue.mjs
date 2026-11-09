// Serial queue: requests and events are handled one at a time, to the end.
// A failing job does not stop the next one.

export class SerialQueue {
  #tail = Promise.resolve();
  #pending = 0;

  enqueue(fn) {
    this.#pending += 1;
    const job = this.#tail.then(() => fn());
    this.#tail = job.then(
      () => {
        this.#pending -= 1;
      },
      () => {
        this.#pending -= 1;
      },
    );
    return job;
  }

  // Resolves when the queue is empty (used by tests and shutdown)
  async idle() {
    for (;;) {
      const tail = this.#tail;
      await tail;
      if (tail === this.#tail) return;
    }
  }

  get pending() {
    return this.#pending;
  }
}

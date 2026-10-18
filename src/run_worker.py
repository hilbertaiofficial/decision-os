"""Run separately from the web process; both connect to the same Firestore database."""
from dotenv import load_dotenv
from decisionos.store import Store
from decisionos.service import Service
from decisionos.worker import Worker
load_dotenv()
if __name__=='__main__':
    worker=Worker(Service(Store.from_env()))
    try:worker.loop()
    except KeyboardInterrupt:worker.stop_event.set()

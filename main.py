from src.app import TreadmillApp
import argparse

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="PitPat Treadmill Control Dashboard")
    parser.add_argument("--host", default="0.0.0.0", help="Host address to listen on")
    parser.add_argument("--port", type=int, default=8050, help="Port to listen on")
    parser.add_argument("--debug", action="store_true", help="Enable debug mode")
    return parser.parse_args()

if __name__ == "__main__":
    args = parse_args()
    app = TreadmillApp(host=args.host, port=args.port, debug=args.debug)
    app.run()
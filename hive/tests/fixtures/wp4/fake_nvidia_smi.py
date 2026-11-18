# fake nvidia-smi (WP4 tests): answers --query-gpu / --query-compute-apps from the fixture files next to it and
# prints the plain header otherwise; touches $FAKE_SMI_MARK when run (proves it was executed)
import os, sys
here = os.environ.get("FAKE_SMI_DIR") or os.path.dirname(os.path.abspath(__file__))
mark = os.environ.get("FAKE_SMI_MARK")
if mark:
    open(mark, "a").write(" ".join(sys.argv[1:]) + "\n")
args = " ".join(sys.argv[1:])
def show(name):
    with open(os.path.join(here, name), encoding="utf-8") as f:
        sys.stdout.write(f.read())
if "--query-gpu" in args:
    show("nvidia_query_gpu.csv")
elif "--version" in args:
    print("NVIDIA-SMI version  : 560.94")
elif "--query-compute-apps" in args:
    show("nvidia_compute_apps.csv")
else:
    show("nvidia_smi_header.txt")

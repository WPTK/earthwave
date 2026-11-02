# shared helpers; source me (copied from eyewall spike S4)
: "${OUT:=$PWD/out}"; mkdir -p "$OUT"
log(){ echo "[$(date +%T)] $*" | tee -a "$OUT/steps.log"; }
run(){ echo "\$ $*"; "$@"; echo "rc=$?"; }

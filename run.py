from mopsimcbot import run
import sys
import os


if __name__ == "__main__":
    token = sys.argv[1] or os.environ.get("MOPSIMCBOT")
    
    run(
        token=token, 
        simc_path="C:/Users/peder/Desktop/simc-548-8-win64/simc64.exe"
        )
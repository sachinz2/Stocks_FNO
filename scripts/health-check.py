#!/usr/bin/env python3
"""
Health Check Script for Falcon Quant Platform
Tests all services before deployment
"""

import requests
import sys
import time
from typing import Dict, Tuple

class HealthChecker:
    def __init__(self, api_host: str = "http://localhost:8000"):
        self.api_host = api_host
        self.results: Dict[str, bool] = {}
        
    def check_api(self) -> bool:
        """Check API health endpoint"""
        try:
            response = requests.get(f"{self.api_host}/api/v1/health", timeout=5)
            if response.status_code == 200:
                data = response.json()
                print(f"✅ API Status: {data.get('status')}")
                return True
            else:
                print(f"❌ API returned status {response.status_code}")
                return False
        except requests.exceptions.ConnectionError:
            print("❌ API: Connection refused")
            return False
        except Exception as e:
            print(f"❌ API: {str(e)}")
            return False
    
    def check_database(self) -> bool:
        """Check database connectivity"""
        try:
            response = requests.get(f"{self.api_host}/api/v1/health", timeout=5)
            if response.status_code == 200:
                data = response.json()
                db_status = data.get('database', 'UNKNOWN')
                print(f"✅ Database Status: {db_status}")
                return db_status == "UP"
            return False
        except Exception as e:
            print(f"❌ Database: {str(e)}")
            return False
    
    def check_redis(self) -> bool:
        """Check Redis connectivity"""
        try:
            response = requests.get(f"{self.api_host}/api/v1/health", timeout=5)
            if response.status_code == 200:
                data = response.json()
                redis_status = data.get('redis', 'UNKNOWN')
                print(f"✅ Redis Status: {redis_status}")
                return redis_status == "UP"
            return False
        except Exception as e:
            print(f"❌ Redis: {str(e)}")
            return False
    
    def run_all_checks(self, retries: int = 5, delay: int = 2) -> Tuple[bool, Dict[str, bool]]:
        """Run all health checks with retries"""
        print("\n" + "="*50)
        print("Falcon Quant Platform - Health Check")
        print("="*50 + "\n")
        
        for attempt in range(1, retries + 1):
            print(f"Attempt {attempt}/{retries}...")
            
            api_ok = self.check_api()
            if not api_ok:
                if attempt < retries:
                    print(f"⏳ Retrying in {delay} seconds...\n")
                    time.sleep(delay)
                continue
            
            db_ok = self.check_database()
            redis_ok = self.check_redis()
            
            self.results = {
                'api': api_ok,
                'database': db_ok,
                'redis': redis_ok
            }
            
            break
        
        return self._print_summary()
    
    def _print_summary(self) -> Tuple[bool, Dict[str, bool]]:
        """Print summary of all checks"""
        print("\n" + "="*50)
        print("Summary")
        print("="*50)
        
        all_ok = all(self.results.values())
        
        for service, status in self.results.items():
            symbol = "✅" if status else "❌"
            print(f"{symbol} {service.upper()}: {'PASS' if status else 'FAIL'}")
        
        print("="*50)
        
        if all_ok:
            print("✅ All health checks passed!")
            return True, self.results
        else:
            print("❌ Some health checks failed")
            return False, self.results

def main():
    import argparse
    
    parser = argparse.ArgumentParser(description="Health check for Falcon Quant Platform")
    parser.add_argument(
        "--host",
        default="http://localhost:8000",
        help="API host (default: http://localhost:8000)"
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=5,
        help="Number of retries (default: 5)"
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=2,
        help="Delay between retries in seconds (default: 2)"
    )
    
    args = parser.parse_args()
    
    checker = HealthChecker(api_host=args.host)
    success, results = checker.run_all_checks(retries=args.retries, delay=args.delay)
    
    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()

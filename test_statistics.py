#!/usr/bin/env python3
"""
Test script for the statistics functionality
"""

import os
import sys
from app import app, create_tables

def test_statistics():
    """Test the statistics page generation"""
    with app.app_context():
        try:
            # Test the statistics route
            with app.test_client() as client:
                response = client.get('/statistics')
                print(f"Statistics page status code: {response.status_code}")
                
                if response.status_code == 200:
                    print("✅ Statistics page loads successfully")
                    print(f"Response length: {len(response.data)} bytes")
                else:
                    print("❌ Statistics page failed to load")
                    print(f"Response: {response.data.decode()}")
                    
        except Exception as e:
            print(f"❌ Error testing statistics: {str(e)}")
            import traceback
            traceback.print_exc()

if __name__ == '__main__':
    print("Testing statistics functionality...")
    test_statistics()